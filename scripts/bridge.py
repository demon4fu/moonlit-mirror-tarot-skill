#!/usr/bin/env python3
"""Local-only session bridge for the Moonlit Mirror tarot skill.

The bridge serves the offline picker, accepts one structured draw, and lets the
current Codex task publish one validated interpretation back to the same page.
It uses only the Python standard library and never contacts an external host.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hmac
import http.client
import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator
import urllib.parse
import webbrowser

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


SCHEMA_VERSION = 1
HOST = "127.0.0.1"
SESSION_ROOT = Path(tempfile.gettempdir()) / "moonlit-mirror-tarot"
SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = SKILL_ROOT / "assets" / "picker"
DATA_PATH = ASSET_ROOT / "data.json"
MAX_REQUEST_BYTES = 2_000_000
DEFAULT_TTL_SECONDS = 30 * 60
ACK_CLEANUP_DELAY_SECONDS = 1.5
PROHIBITED_PATTERNS = (
    re.compile(r"(?<!不)一定会"),
    re.compile(r"注定(?:会|要|发生|成为|失去|得到)"),
    re.compile(r"必然(?:会|发生|导致|结果)"),
    re.compile(r"(?:成功率|准确率|概率)\s*(?:是|为|达到|高达)?\s*\d"),
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(r"(?:他|她|对方)(?:内心|心里)(?:就是|一定|肯定)"),
)


class BridgeError(RuntimeError):
    """Expected validation or session error."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def session_lock(session_dir: Path) -> Iterator[None]:
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / ".lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_domain_data() -> dict[str, Any]:
    if not DATA_PATH.is_file():
        raise BridgeError(
            "离线牌组尚未构建。请在技能源码项目中先运行 npm run skill:build。"
        )
    value = read_json(DATA_PATH)
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != SCHEMA_VERSION
        or len(value.get("cards", [])) != 78
        or len(value.get("spreads", [])) != 9
    ):
        raise BridgeError("离线牌组数据不完整。")
    return value


def require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError(f"{field} 必须是对象。")
    return value


def require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{field} 缺失。")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise BridgeError(f"{field} 过长。")
    return normalized


def require_text_list(
    value: Any,
    field: str,
    *,
    min_items: int,
    max_items: int,
    max_length: int,
) -> list[str]:
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise BridgeError(f"{field} 数量不正确。")
    return [
        require_text(item, f"{field}[{index}]", max_length)
        for index, item in enumerate(value)
    ]


def validate_selection(value: Any, domain: dict[str, Any]) -> dict[str, Any]:
    payload = require_dict(value, "抽牌数据")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise BridgeError("抽牌数据版本不受支持。")
    if payload.get("product") != "月下镜语":
        raise BridgeError("抽牌数据来源不正确。")

    reading_id = require_text(payload.get("readingId"), "readingId", 160)
    created_at = require_text(payload.get("createdAt"), "createdAt", 80)
    try:
        parse_time(created_at)
    except ValueError as error:
        raise BridgeError("createdAt 不是有效时间。") from error

    valid_topics = {item["id"] for item in domain["topics"]}
    topic = require_text(payload.get("topic"), "topic", 40)
    if topic not in valid_topics:
        raise BridgeError("抽牌主题无效。")

    question = payload.get("question")
    if question is not None:
        if not isinstance(question, str) or len(question.strip()) > 200:
            raise BridgeError("问题必须是不超过 200 字的文本。")
        question = question.strip() or None

    reversals_enabled = payload.get("reversalsEnabled")
    if not isinstance(reversals_enabled, bool):
        raise BridgeError("reversalsEnabled 必须是布尔值。")

    spread_input = require_dict(payload.get("spread"), "spread")
    spread_id = require_text(spread_input.get("id"), "spread.id", 80)
    spreads = {item["id"]: item for item in domain["spreads"]}
    spread = spreads.get(spread_id)
    if spread is None:
        raise BridgeError("牌阵不存在。")
    for key in ("name", "nameEn", "description"):
        if spread_input.get(key) != spread[key]:
            raise BridgeError(f"spread.{key} 与离线牌阵不匹配。")

    cards_input = payload.get("cards")
    if not isinstance(cards_input, list) or len(cards_input) != len(spread["positions"]):
        raise BridgeError("抽牌数量与牌阵不一致。")

    cards = {item["id"]: item for item in domain["cards"]}
    seen_cards: set[str] = set()
    normalized_cards: list[dict[str, Any]] = []
    for index, (raw_card, position) in enumerate(
        zip(cards_input, spread["positions"])
    ):
        item = require_dict(raw_card, f"cards[{index}]")
        card_id = require_text(item.get("cardId"), f"cards[{index}].cardId", 100)
        card = cards.get(card_id)
        if card is None or card_id in seen_cards:
            raise BridgeError("抽牌中包含未知或重复卡牌。")
        seen_cards.add(card_id)
        orientation = require_text(
            item.get("orientation"), f"cards[{index}].orientation", 20
        )
        if orientation not in ("upright", "reversed"):
            raise BridgeError("卡牌方向无效。")
        if not reversals_enabled and orientation != "upright":
            raise BridgeError("关闭逆位时不能出现逆位牌。")

        exact_fields = {
            "positionId": position["id"],
            "positionName": position["name"],
            "positionPrompt": position["prompt"],
            "nameZh": card["nameZh"],
            "nameEn": card["nameEn"],
            "arcana": card["arcana"],
            "suit": card["suit"],
            "element": card["element"],
            "number": card["number"],
            "traditionalSymbols": card["traditionalSymbols"],
            "keywords": card["keywords"][orientation],
        }
        if card.get("rank") is not None:
            exact_fields["rank"] = card["rank"]
        for key, expected in exact_fields.items():
            if item.get(key) != expected:
                raise BridgeError(f"cards[{index}].{key} 与离线牌组不匹配。")

        normalized_card = {
            "positionId": position["id"],
            "positionName": position["name"],
            "positionPrompt": position["prompt"],
            "cardId": card_id,
            "nameZh": card["nameZh"],
            "nameEn": card["nameEn"],
            "orientation": orientation,
            "arcana": card["arcana"],
            "suit": card["suit"],
            "element": card["element"],
            "number": card["number"],
            "traditionalSymbols": list(card["traditionalSymbols"]),
            "keywords": list(card["keywords"][orientation]),
        }
        if card.get("rank") is not None:
            normalized_card["rank"] = card["rank"]
        normalized_cards.append(normalized_card)

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "product": "月下镜语",
        "readingId": reading_id,
        "createdAt": created_at,
        "topic": topic,
        "reversalsEnabled": reversals_enabled,
        "spread": {
            "id": spread["id"],
            "name": spread["name"],
            "nameEn": spread["nameEn"],
            "description": spread["description"],
        },
        "cards": normalized_cards,
    }
    if question:
        result["question"] = question
    return result


def validate_interpretation(
    value: Any, selection: dict[str, Any]
) -> dict[str, Any]:
    payload = require_dict(value, "模型结果")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise BridgeError("模型结果版本不受支持。")
    if payload.get("readingId") != selection["readingId"]:
        raise BridgeError("模型结果与当前抽牌记录不匹配。")

    raw_insights = payload.get("cardInsights")
    expected_cards = {
        card["positionId"]: card["cardId"] for card in selection["cards"]
    }
    if not isinstance(raw_insights, list) or len(raw_insights) != len(expected_cards):
        raise BridgeError("cardInsights 必须覆盖每一个牌位。")

    seen_positions: set[str] = set()
    insights: list[dict[str, str]] = []
    for index, raw_item in enumerate(raw_insights):
        item = require_dict(raw_item, f"cardInsights[{index}]")
        position_id = require_text(
            item.get("positionId"), f"cardInsights[{index}].positionId", 80
        )
        card_id = require_text(
            item.get("cardId"), f"cardInsights[{index}].cardId", 100
        )
        if (
            position_id in seen_positions
            or expected_cards.get(position_id) != card_id
        ):
            raise BridgeError("cardInsights 中的牌位或卡牌对应关系错误。")
        seen_positions.add(position_id)
        insights.append(
            {
                "positionId": position_id,
                "cardId": card_id,
                "title": require_text(
                    item.get("title"), f"cardInsights[{index}].title", 120
                ),
                "interpretation": require_text(
                    item.get("interpretation"),
                    f"cardInsights[{index}].interpretation",
                    1600,
                ),
            }
        )

    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "readingId": selection["readingId"],
        "headline": require_text(payload.get("headline"), "headline", 120),
        "summary": require_text(payload.get("summary"), "summary", 1800),
        "cardInsights": insights,
        "relationships": require_text_list(
            payload.get("relationships"),
            "relationships",
            min_items=1,
            max_items=8,
            max_length=900,
        ),
        "actions": require_text_list(
            payload.get("actions"),
            "actions",
            min_items=1,
            max_items=5,
            max_length=500,
        ),
        "pauses": require_text_list(
            payload.get("pauses"),
            "pauses",
            min_items=1,
            max_items=5,
            max_length=500,
        ),
        "reflections": require_text_list(
            payload.get("reflections"),
            "reflections",
            min_items=1,
            max_items=6,
            max_length=500,
        ),
        "boundaryNote": require_text(
            payload.get("boundaryNote"), "boundaryNote", 700
        ),
    }

    searchable = json.dumps(result, ensure_ascii=False)
    for pattern in PROHIBITED_PATTERNS:
        if pattern.search(searchable):
            raise BridgeError("模型结果含有确定性预言、概率或读心式表述。")
    return result


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": state["schemaVersion"],
        "sessionId": state["sessionId"],
        "status": state["status"],
        "version": state["version"],
        "createdAt": state["createdAt"],
        "updatedAt": state["updatedAt"],
        "expiresAt": state["expiresAt"],
        "selection": state.get("selection"),
        "interpretation": state.get("interpretation"),
        "lastError": state.get("lastError"),
    }


def resolve_session_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    root = SESSION_ROOT.resolve()
    if path.name != "session.json" or path.parent.parent != root:
        raise BridgeError("会话文件不在月下镜语的临时目录中。")
    return path


def read_session_files(session_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not session_path.is_file():
        raise BridgeError("会话已经清理或不存在。")
    descriptor = read_json(session_path)
    state_path = session_path.parent / "state.json"
    if not state_path.is_file():
        raise BridgeError("会话状态不存在。")
    return descriptor, read_json(state_path)


def update_state(
    session_dir: Path,
    updater: Any,
) -> dict[str, Any]:
    state_path = session_dir / "state.json"
    with session_lock(session_dir):
        if not state_path.is_file():
            raise BridgeError("会话已经清理。")
        state = read_json(state_path)
        updated = updater(state)
        updated["version"] = int(updated.get("version", 0)) + 1
        updated["updatedAt"] = isoformat(utc_now())
        atomic_write_json(state_path, updated)
        return updated


def authenticated_request(
    descriptor: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any] | None]:
    port = descriptor.get("port")
    token = descriptor.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        raise BridgeError("会话服务尚未就绪。")
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        value = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, value
    finally:
        connection.close()


def cleanup_stale_sessions() -> None:
    SESSION_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SESSION_ROOT, 0o700)
    now = utc_now()
    for candidate in SESSION_ROOT.iterdir():
        if not candidate.is_dir():
            continue
        descriptor_path = candidate / "session.json"
        try:
            descriptor = read_json(descriptor_path)
            expires_at = parse_time(descriptor["expiresAt"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            if now.timestamp() - candidate.stat().st_mtime > 3600:
                shutil.rmtree(candidate, ignore_errors=True)
            continue
        if expires_at > now:
            continue
        try:
            authenticated_request(
                descriptor,
                "POST",
                "/api/close",
                {"reason": "stale_session"},
                timeout=0.5,
            )
        except (OSError, BridgeError, http.client.HTTPException):
            shutil.rmtree(candidate, ignore_errors=True)


class LocalSessionServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        *,
        session_dir: Path,
        token: str,
        expires_at: dt.datetime,
        domain: dict[str, Any],
    ) -> None:
        self.session_dir = session_dir
        self.token = token
        self.expires_at = expires_at
        self.domain = domain
        self.cleanup_deadline: float | None = None
        self.state_guard = threading.Lock()
        super().__init__(address, handler_class)

    def schedule_cleanup(self) -> None:
        self.cleanup_deadline = time.monotonic() + ACK_CLEANUP_DELAY_SECONDS


class SessionHandler(http.server.BaseHTTPRequestHandler):
    server: LocalSessionServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _security_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; connect-src 'self'; img-src 'self'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _send_bytes(
        self, status: int, value: bytes, content_type: str
    ) -> None:
        self.send_response(status)
        self._security_headers(content_type, len(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(value)

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :], self.server.token)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected = f"http://{HOST}:{self.server.server_port}"
        return origin == expected

    def _require_api_access(self) -> bool:
        if not self._origin_allowed():
            self._send_json(403, {"error": "请求来源被拒绝。"})
            return False
        if not self._authorized():
            self._send_json(403, {"error": "会话令牌无效。"})
            return False
        return True

    def _read_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise BridgeError("请求缺少 Content-Length。")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise BridgeError("Content-Length 无效。") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise BridgeError("请求体过大。")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BridgeError("请求不是有效 JSON。") from error
        return require_dict(value, "请求体")

    def _state(self) -> dict[str, Any]:
        with self.server.state_guard:
            state_path = self.server.session_dir / "state.json"
            if not state_path.is_file():
                raise BridgeError("会话已经清理。")
            return read_json(state_path)

    def _update(self, updater: Any) -> dict[str, Any]:
        with self.server.state_guard:
            return update_state(self.server.session_dir, updater)

    def do_HEAD(self) -> None:
        self._serve_get(send_body=False)

    def do_GET(self) -> None:
        self._serve_get(send_body=True)

    def _serve_get(self, *, send_body: bool) -> None:
        del send_body
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if not self._require_api_access():
                return
            try:
                if parsed.path == "/api/health":
                    self._send_json(200, {"status": "ok"})
                elif parsed.path == "/api/state":
                    self._send_json(200, public_state(self._state()))
                else:
                    self._send_json(404, {"error": "接口不存在。"})
            except BridgeError as error:
                self._send_json(410, {"error": str(error)})
            return

        relative = "index.html" if parsed.path in ("", "/") else parsed.path.lstrip("/")
        candidate = (ASSET_ROOT / relative).resolve()
        try:
            candidate.relative_to(ASSET_ROOT.resolve())
        except ValueError:
            self._send_json(404, {"error": "文件不存在。"})
            return
        if not candidate.is_file():
            self._send_json(404, {"error": "文件不存在。"})
            return
        mime_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if mime_type.startswith("text/") or mime_type in (
            "application/javascript",
            "application/json",
        ):
            mime_type = f"{mime_type}; charset=utf-8"
        self._send_bytes(200, candidate.read_bytes(), mime_type)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "接口不存在。"})
            return
        if not self._require_api_access():
            return
        try:
            body = self._read_body()
            if parsed.path == "/api/selection":
                selection = validate_selection(body, self.server.domain)

                def select(current: dict[str, Any]) -> dict[str, Any]:
                    if current["status"] != "waiting_for_draw":
                        raise BridgeError("当前会话不能重复提交抽牌。")
                    current["selection"] = selection
                    current["status"] = "waiting_for_model"
                    current["lastError"] = None
                    return current

                updated = self._update(select)
                self._send_json(200, public_state(updated))
            elif parsed.path == "/api/ack":
                reading_id = require_text(body.get("readingId"), "readingId", 160)

                def acknowledge(current: dict[str, Any]) -> dict[str, Any]:
                    if (
                        current["status"] != "completed"
                        or not current.get("interpretation")
                        or current["interpretation"].get("readingId") != reading_id
                    ):
                        raise BridgeError("当前结果不能确认。")
                    current["acknowledgedAt"] = isoformat(utc_now())
                    return current

                updated = self._update(acknowledge)
                self._send_json(200, {"status": updated["status"], "acknowledged": True})
                self.server.schedule_cleanup()
            elif parsed.path in ("/api/cancel", "/api/close"):
                reason = body.get("reason")
                if not isinstance(reason, str) or len(reason) > 80:
                    reason = "cancelled"

                def cancel(current: dict[str, Any]) -> dict[str, Any]:
                    if current["status"] != "completed":
                        current["status"] = "cancelled"
                    current["closedReason"] = reason
                    return current

                updated = self._update(cancel)
                self._send_json(200, {"status": updated["status"]})
                self.server.schedule_cleanup()
            else:
                self._send_json(404, {"error": "接口不存在。"})
        except BridgeError as error:
            self._send_json(422, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return


def initial_state(
    session_id: str, created_at: dt.datetime, expires_at: dt.datetime
) -> dict[str, Any]:
    timestamp = isoformat(created_at)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session_id,
        "status": "waiting_for_draw",
        "version": 0,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "expiresAt": isoformat(expires_at),
        "selection": None,
        "interpretation": None,
        "lastError": None,
        "acknowledgedAt": None,
    }


def run_server(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir).resolve()
    session_path = resolve_session_path(str(session_dir / "session.json"))
    descriptor = read_json(session_path)
    token = descriptor.get("token")
    if not isinstance(token, str) or len(token) < 32:
        raise BridgeError("会话令牌无效。")
    expires_at = parse_time(descriptor["expiresAt"])
    domain = load_domain_data()
    server = LocalSessionServer(
        (HOST, 0),
        SessionHandler,
        session_dir=session_dir,
        token=token,
        expires_at=expires_at,
        domain=domain,
    )
    server.timeout = 0.4
    with session_lock(session_dir):
        descriptor = read_json(session_path)
        descriptor["port"] = server.server_port
        descriptor["pid"] = os.getpid()
        descriptor["readyAt"] = isoformat(utc_now())
        atomic_write_json(session_path, descriptor)

    try:
        while utc_now() < expires_at:
            server.handle_request()
            if (
                server.cleanup_deadline is not None
                and time.monotonic() >= server.cleanup_deadline
            ):
                break
        else:
            try:
                update_state(
                    session_dir,
                    lambda current: {
                        **current,
                        "status": "expired",
                        "selection": None,
                        "interpretation": None,
                        "lastError": None,
                    },
                )
            except BridgeError:
                pass
    finally:
        server.server_close()
        shutil.rmtree(session_dir, ignore_errors=True)
    return 0


def start_session(args: argparse.Namespace) -> int:
    load_domain_data()
    cleanup_stale_sessions()
    created_at = utc_now()
    expires_at = created_at + dt.timedelta(seconds=args.ttl)
    session_id = f"{created_at.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}"
    session_dir = SESSION_ROOT / session_id
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    session_path = session_dir / "session.json"
    token = secrets.token_urlsafe(32)
    descriptor: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "sessionId": session_id,
        "token": token,
        "host": HOST,
        "port": None,
        "pid": None,
        "createdAt": isoformat(created_at),
        "expiresAt": isoformat(expires_at),
        "skillRoot": str(SKILL_ROOT),
    }
    atomic_write_json(session_path, descriptor)
    atomic_write_json(
        session_dir / "state.json",
        initial_state(session_id, created_at, expires_at),
    )

    error_log = (session_dir / "service-errors.log").open("ab")
    os.chmod(session_dir / "service-errors.log", 0o600)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                "--session-dir",
                str(session_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=error_log,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        error_log.close()
    deadline = time.monotonic() + 10
    ready_descriptor: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            candidate = read_json(session_path)
            if isinstance(candidate.get("port"), int):
                status, _ = authenticated_request(
                    candidate, "GET", "/api/health", timeout=0.4
                )
                if status == 200:
                    ready_descriptor = candidate
                    break
        except (
            OSError,
            BridgeError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ):
            pass
        time.sleep(0.08)

    if ready_descriptor is None:
        if process.poll() is None:
            process.terminate()
        shutil.rmtree(session_dir, ignore_errors=True)
        raise BridgeError("本地牌桌服务未能启动。")

    port = ready_descriptor["port"]
    url = f"http://{HOST}:{port}/#token={urllib.parse.quote(token, safe='')}"
    opened = False
    if not args.no_open:
        opened = bool(webbrowser.open(url, new=1, autoraise=True))
    output = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "waiting_for_draw",
        "sessionId": session_id,
        "sessionFile": str(session_path),
        "url": url,
        "browserOpened": opened,
        "expiresAt": ready_descriptor["expiresAt"],
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


def wait_for_draw(args: argparse.Namespace) -> int:
    session_path = resolve_session_path(args.session)
    deadline = None if args.timeout == 0 else time.monotonic() + args.timeout
    while deadline is None or time.monotonic() < deadline:
        try:
            _descriptor, state = read_session_files(session_path)
        except BridgeError:
            raise BridgeError("会话已结束，未取得可解读的抽牌。")
        if state["status"] == "waiting_for_model" and state.get("selection"):
            print(json.dumps(state["selection"], ensure_ascii=False, indent=2))
            return 0
        if state["status"] in ("cancelled", "expired"):
            raise BridgeError(f"会话状态为 {state['status']}。")
        if state["status"] == "completed":
            raise BridgeError("这次会话已经发布过解读。")
        time.sleep(0.25)
    raise BridgeError("等待抽牌超时；牌桌仍可在会话有效期内继续使用。")


def load_publish_input(path: str) -> Any:
    if path == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as error:
            raise BridgeError("标准输入不是有效 JSON。") from error
    input_path = Path(path).expanduser().resolve()
    try:
        return read_json(input_path)
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError("无法读取模型结果 JSON。") from error


def mark_publish_error(session_dir: Path) -> None:
    try:
        update_state(
            session_dir,
            lambda current: {
                **current,
                "lastError": "模型结果未通过格式校验，可修正后直接重新发布。",
                "errorCount": int(current.get("errorCount", 0)) + 1,
            },
        )
    except BridgeError:
        pass


def publish_interpretation(args: argparse.Namespace) -> int:
    session_path = resolve_session_path(args.session)
    descriptor, current = read_session_files(session_path)
    del descriptor
    if current["status"] != "waiting_for_model" or not current.get("selection"):
        raise BridgeError("当前会话没有等待解读的抽牌。")
    try:
        raw_payload = load_publish_input(args.input)
        interpretation = validate_interpretation(raw_payload, current["selection"])
    except BridgeError:
        mark_publish_error(session_path.parent)
        raise

    def publish(state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] != "waiting_for_model" or not state.get("selection"):
            raise BridgeError("当前会话状态已经改变。")
        state["interpretation"] = interpretation
        state["status"] = "completed"
        state["lastError"] = None
        state["publishedAt"] = isoformat(utc_now())
        return state

    update_state(session_path.parent, publish)

    delivered = False
    if args.ack_timeout > 0:
        deadline = time.monotonic() + args.ack_timeout
        while time.monotonic() < deadline:
            if not session_path.exists():
                delivered = True
                break
            try:
                _descriptor, latest = read_session_files(session_path)
                if latest.get("acknowledgedAt"):
                    delivered = True
                    break
            except BridgeError:
                delivered = True
                break
            time.sleep(0.2)
    print(
        json.dumps(
            {
                "status": "completed",
                "readingId": interpretation["readingId"],
                "deliveredToPage": delivered,
            },
            ensure_ascii=False,
        )
    )
    return 0


def close_session(args: argparse.Namespace) -> int:
    session_path = resolve_session_path(args.session)
    if not session_path.exists():
        print(json.dumps({"status": "already_closed"}, ensure_ascii=False))
        return 0
    descriptor = read_json(session_path)
    try:
        authenticated_request(
            descriptor,
            "POST",
            "/api/close",
            {"reason": args.reason},
            timeout=1.0,
        )
    except (OSError, BridgeError, http.client.HTTPException):
        pid = descriptor.get("pid")
        if isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(session_path.parent, ignore_errors=True)
    deadline = time.monotonic() + 4
    while session_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if session_path.exists():
        shutil.rmtree(session_path.parent, ignore_errors=True)
    print(json.dumps({"status": "closed"}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="月下镜语离线牌桌与当前模型之间的本机会话桥。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="启动牌桌并打开默认浏览器。")
    start.add_argument(
        "--ttl",
        type=int,
        default=DEFAULT_TTL_SECONDS,
        choices=range(5, 7201),
        metavar="SECONDS",
        help="会话有效期，5 到 7200 秒；默认 1800 秒。",
    )
    start.add_argument(
        "--no-open",
        action="store_true",
        help="只启动服务，不打开浏览器（用于自动测试）。",
    )
    start.set_defaults(handler=start_session)

    serve = subparsers.add_parser("serve", help=argparse.SUPPRESS)
    serve.add_argument("--session-dir", required=True)
    serve.set_defaults(handler=run_server)

    wait = subparsers.add_parser("wait", help="等待用户完成抽牌并输出结构化数据。")
    wait.add_argument("--session", required=True, help="start 返回的 sessionFile。")
    wait.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="等待秒数；0 表示一直等待到会话结束。",
    )
    wait.set_defaults(handler=wait_for_draw)

    publish = subparsers.add_parser("publish", help="校验并回传模型解读。")
    publish.add_argument("--session", required=True, help="start 返回的 sessionFile。")
    publish.add_argument(
        "--input",
        required=True,
        help="模型结果 JSON 文件；使用 - 从标准输入读取。",
    )
    publish.add_argument(
        "--ack-timeout",
        type=float,
        default=20.0,
        help="等待网页确认收到结果的秒数；0 表示不等待。",
    )
    publish.set_defaults(handler=publish_interpretation)

    close = subparsers.add_parser("close", help="结束会话并清理临时数据。")
    close.add_argument("--session", required=True, help="start 返回的 sessionFile。")
    close.add_argument("--reason", default="task_closed")
    close.set_defaults(handler=close_session)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except BridgeError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
