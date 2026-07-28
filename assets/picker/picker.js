(() => {
  "use strict";

  const TOKEN_KEY = "moonlit-mirror-session-token";
  const TOPIC_LABELS = {
    general: "综合",
    growth: "成长",
    relationship: "关系",
    career: "事业",
    choice: "选择",
  };
  const ORIENTATION_LABELS = {
    upright: "正位",
    reversed: "逆位",
  };

  const hash = new URLSearchParams(window.location.hash.slice(1));
  const hashToken = hash.get("token");
  if (hashToken) {
    window.sessionStorage.setItem(TOKEN_KEY, hashToken);
    window.history.replaceState(null, "", "/");
  }
  const sessionToken = hashToken || window.sessionStorage.getItem(TOKEN_KEY) || "";

  const state = {
    data: null,
    sessionId: "",
    sessionStatus: "waiting_for_draw",
    selectedTopic: "general",
    selectedSpreadId: "action-three",
    reversalsEnabled: true,
    question: "",
    deck: [],
    selected: [],
    selectedDeckIndexes: new Set(),
    revealed: new Set(),
    readingPayload: null,
    pollTimer: 0,
    resultRendered: false,
    toastTimer: 0,
  };

  const views = Object.fromEntries(
    [...document.querySelectorAll(".view")].map((node) => [node.id, node]),
  );
  const form = document.querySelector("#reading-form");
  const question = document.querySelector("#question");
  const questionCount = document.querySelector("#question-count");
  const reversals = document.querySelector("#reversals");
  const topicOptions = document.querySelector("#topic-options");
  const spreadOptions = document.querySelector("#spread-options");
  const cardWheel = document.querySelector("#card-wheel");
  const drawSlots = document.querySelector("#draw-slots");
  const positionNav = document.querySelector("#position-nav");
  const drawCount = document.querySelector("#draw-count");
  const drawTotal = document.querySelector("#draw-total");
  const drawProgressBar = document.querySelector("#draw-progress-bar");
  const drawInstruction = document.querySelector("#draw-instruction");
  const autoComplete = document.querySelector("#auto-complete");
  const toReveal = document.querySelector("#to-reveal");
  const revealCards = document.querySelector("#reveal-cards");
  const revealPositionNav = document.querySelector("#reveal-position-nav");
  const revealCount = document.querySelector("#reveal-count");
  const revealTotal = document.querySelector("#reveal-total");
  const revealAll = document.querySelector("#reveal-all");
  const sendReading = document.querySelector("#send-reading");
  const waitingError = document.querySelector("#waiting-error");
  const checkResult = document.querySelector("#check-result");
  const cancelSession = document.querySelector("#cancel-session");
  const toast = document.querySelector("#toast");

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 3200);
  }

  function showView(id) {
    for (const [viewId, node] of Object.entries(views)) {
      const active = viewId === id;
      node.hidden = !active;
      node.classList.toggle("is-active", active);
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    window.requestAnimationFrame(() => {
      const heading = views[id]?.querySelector("h1");
      if (heading) heading.focus({ preventScroll: true });
    });
  }

  function cryptoIndex(maxExclusive) {
    if (!window.crypto?.getRandomValues) {
      throw new Error("当前浏览器不支持安全随机源。");
    }
    if (maxExclusive <= 0 || maxExclusive > 0x1_0000_0000) {
      throw new Error("随机范围无效。");
    }
    const limit = Math.floor(0x1_0000_0000 / maxExclusive) * maxExclusive;
    const value = new Uint32Array(1);
    do {
      window.crypto.getRandomValues(value);
    } while (value[0] >= limit);
    return value[0] % maxExclusive;
  }

  function secureShuffle(values) {
    const shuffled = [...values];
    for (let index = shuffled.length - 1; index > 0; index -= 1) {
      const swapIndex = cryptoIndex(index + 1);
      [shuffled[index], shuffled[swapIndex]] = [
        shuffled[swapIndex],
        shuffled[index],
      ];
    }
    return shuffled;
  }

  function randomOrientation() {
    return state.reversalsEnabled && cryptoIndex(2) === 0 ? "reversed" : "upright";
  }

  function currentSpread() {
    return state.data.spreads.find((spread) => spread.id === state.selectedSpreadId);
  }

  function cardById(cardId) {
    return state.data.cards.find((card) => card.id === cardId);
  }

  function createRadio(name, value, checked) {
    const input = document.createElement("input");
    input.type = "radio";
    input.name = name;
    input.value = value;
    input.checked = checked;
    return input;
  }

  function renderSetupOptions() {
    topicOptions.replaceChildren();
    for (const topic of state.data.topics) {
      const label = element("label", "choice-input");
      const input = createRadio("topic", topic.id, topic.id === state.selectedTopic);
      const visual = element("span", "topic-option", topic.name);
      label.append(input, visual);
      topicOptions.append(label);
    }

    spreadOptions.replaceChildren();
    for (const spread of state.data.spreads) {
      const label = element("label", "choice-input");
      const input = createRadio(
        "spread",
        spread.id,
        spread.id === state.selectedSpreadId,
      );
      const visual = element("span", "spread-option");
      const header = element("span", "spread-option-header");
      header.append(
        element("b", "", spread.name),
        element("em", "", `${spread.positions.length} 张`),
      );
      const description = element("p", "", spread.description);
      const mini = element("span", "mini-spread");
      mini.setAttribute("aria-hidden", "true");
      for (let index = 0; index < spread.positions.length; index += 1) {
        mini.append(element("i"));
      }
      visual.append(header, description, mini);
      label.append(input, visual);
      spreadOptions.append(label);
    }
  }

  function setPositionChipState(node, index, completedCount, activeIndex) {
    node.classList.toggle("is-done", index < completedCount);
    node.classList.toggle("is-current", index === activeIndex);
  }

  function renderPositionNav(container, completedCount, activeIndex, revealMode = false) {
    const spread = currentSpread();
    container.replaceChildren();
    spread.positions.forEach((position, index) => {
      const chip = element("button", "position-chip");
      chip.type = "button";
      const dot = element("i");
      const name = element("span", "", position.name);
      chip.append(dot, name);
      setPositionChipState(chip, index, completedCount, activeIndex);
      chip.addEventListener("click", () => {
        const target = revealMode
          ? document.querySelector(`#reveal-position-${CSS.escape(position.id)}`)
          : document.querySelector(`#draw-position-${CSS.escape(position.id)}`);
        target?.scrollIntoView({
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
            ? "auto"
            : "smooth",
          block: "nearest",
          inline: "center",
        });
      });
      container.append(chip);
    });
  }

  function renderDrawSlots() {
    const spread = currentSpread();
    drawSlots.replaceChildren();
    spread.positions.forEach((position, index) => {
      const slot = element("article", "slot");
      slot.id = `draw-position-${position.id}`;
      if (state.selected[index]) slot.classList.add("is-filled");
      if (index === state.selected.length && state.selected.length < spread.positions.length) {
        slot.classList.add("is-current");
      }
      const frame = element("div", "slot-frame");
      const label = element("span", "slot-label", position.name);
      label.append(element("small", "", `${index + 1} / ${spread.positions.length}`));
      slot.append(frame, label);
      drawSlots.append(slot);
    });

    const count = state.selected.length;
    drawCount.textContent = String(count);
    drawTotal.textContent = `/ ${spread.positions.length}`;
    drawProgressBar.style.width = `${(count / spread.positions.length) * 100}%`;
    renderPositionNav(
      positionNav,
      count,
      count < spread.positions.length ? count : -1,
    );
    const nextPosition = spread.positions[count];
    drawInstruction.textContent = nextPosition
      ? `现在选择「${nextPosition.name}」的卡背。`
      : "所有牌位已经完成，可以进入揭示。";
    autoComplete.hidden = spread.positions.length <= 3 || count >= spread.positions.length;
    toReveal.disabled = count !== spread.positions.length;

    if (count > 0) {
      const activeSlot = drawSlots.children[Math.min(count, spread.positions.length - 1)];
      activeSlot?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      });
    }
  }

  function renderCardWheel() {
    cardWheel.replaceChildren();
    state.deck.forEach((card, index) => {
      const button = element("button", "deck-card");
      button.type = "button";
      button.setAttribute("aria-label", `选择第 ${index + 1} 张卡背`);
      const normalized = (index / Math.max(state.deck.length - 1, 1)) * 2 - 1;
      button.style.setProperty("--angle", `${normalized * 5.5}deg`);
      button.style.setProperty("--lift", `${Math.abs(normalized) * 9}px`);
      button.disabled = state.selectedDeckIndexes.has(index);
      button.addEventListener("click", () => selectDeckCard(index, card));
      cardWheel.append(button);
    });
    window.requestAnimationFrame(() => {
      cardWheel.scrollLeft = Math.max(0, (cardWheel.scrollWidth - cardWheel.clientWidth) / 2);
    });
  }

  function selectDeckCard(index, card) {
    const spread = currentSpread();
    if (
      state.selected.length >= spread.positions.length ||
      state.selectedDeckIndexes.has(index)
    ) {
      return;
    }
    state.selectedDeckIndexes.add(index);
    state.selected.push({
      card,
      orientation: randomOrientation(),
      deckIndex: index,
    });
    const button = cardWheel.children[index];
    if (button) button.disabled = true;
    renderDrawSlots();
  }

  function completeRemainingDraw() {
    const spread = currentSpread();
    for (let index = 0; index < state.deck.length; index += 1) {
      if (state.selected.length >= spread.positions.length) break;
      if (!state.selectedDeckIndexes.has(index)) {
        state.selectedDeckIndexes.add(index);
        state.selected.push({
          card: state.deck[index],
          orientation: randomOrientation(),
          deckIndex: index,
        });
        const button = cardWheel.children[index];
        if (button) button.disabled = true;
      }
    }
    renderDrawSlots();
  }

  function renderReveal() {
    const spread = currentSpread();
    revealCards.replaceChildren();
    spread.positions.forEach((position, index) => {
      const selected = state.selected[index];
      const article = element("article", "reveal-card");
      article.id = `reveal-position-${position.id}`;
      const button = element("button", "flip-button");
      button.type = "button";
      button.setAttribute("aria-label", `翻开「${position.name}」`);
      if (state.revealed.has(index)) button.classList.add("is-revealed");
      const inner = element("span", "flip-inner");
      const back = element("span", "flip-face flip-back");
      const front = element(
        "span",
        `flip-face flip-front${selected.orientation === "reversed" ? " is-reversed" : ""}`,
      );
      const image = document.createElement("img");
      image.src = selected.card.image;
      image.alt = `${selected.card.nameZh}，${ORIENTATION_LABELS[selected.orientation]}`;
      image.width = 480;
      image.height = 720;
      front.append(image);
      inner.append(back, front);
      button.append(inner);
      button.addEventListener("click", () => revealCard(index, button));

      const copy = element("div", "reveal-card-copy");
      copy.append(
        element("p", "", `${index + 1} · ${position.name}`),
        element(
          "h2",
          "",
          state.revealed.has(index) ? selected.card.nameZh : "尚未揭示",
        ),
        element(
          "small",
          "",
          state.revealed.has(index)
            ? `${selected.card.nameEn} · ${ORIENTATION_LABELS[selected.orientation]}`
            : position.prompt,
        ),
      );
      article.append(button, copy);
      revealCards.append(article);
    });
    updateRevealProgress();
  }

  function revealCard(index, button) {
    if (state.revealed.has(index)) return;
    state.revealed.add(index);
    button.classList.add("is-revealed");
    const selected = state.selected[index];
    const copy = button.nextElementSibling;
    copy.querySelector("h2").textContent = selected.card.nameZh;
    copy.querySelector("small").textContent =
      `${selected.card.nameEn} · ${ORIENTATION_LABELS[selected.orientation]}`;
    updateRevealProgress();
  }

  function updateRevealProgress() {
    const spread = currentSpread();
    const count = state.revealed.size;
    revealCount.textContent = String(count);
    revealTotal.textContent = `/ ${spread.positions.length}`;
    revealAll.disabled = count === spread.positions.length;
    sendReading.disabled = count !== spread.positions.length;
    const firstPending = spread.positions.findIndex((_, index) => !state.revealed.has(index));
    renderPositionNav(
      revealPositionNav,
      count,
      firstPending,
      true,
    );
  }

  function createReadingPayload() {
    const spread = currentSpread();
    const entropy = new Uint32Array(2);
    window.crypto.getRandomValues(entropy);
    const readingId = `skill-${Date.now().toString(36)}-${[...entropy]
      .map((value) => value.toString(36))
      .join("")}`;
    return {
      schemaVersion: 1,
      product: "月下镜语",
      readingId,
      createdAt: new Date().toISOString(),
      topic: state.selectedTopic,
      question: state.question || undefined,
      reversalsEnabled: state.reversalsEnabled,
      spread: {
        id: spread.id,
        name: spread.name,
        nameEn: spread.nameEn,
        description: spread.description,
      },
      cards: spread.positions.map((position, index) => {
        const selection = state.selected[index];
        const card = selection.card;
        return {
          positionId: position.id,
          positionName: position.name,
          positionPrompt: position.prompt,
          cardId: card.id,
          nameZh: card.nameZh,
          nameEn: card.nameEn,
          orientation: selection.orientation,
          arcana: card.arcana,
          suit: card.suit,
          element: card.element,
          number: card.number,
          ...(card.rank ? { rank: card.rank } : {}),
          traditionalSymbols: [...card.traditionalSymbols],
          keywords: [...card.keywords[selection.orientation]],
        };
      }),
    };
  }

  async function api(path, options = {}) {
    if (!sessionToken) throw new Error("会话令牌缺失，请从当前对话重新运行技能。");
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${sessionToken}`);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await window.fetch(path, {
      ...options,
      headers,
      cache: "no-store",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `本机会话返回 ${response.status}`);
    }
    return response.status === 204 ? null : response.json();
  }

  async function submitReading() {
    sendReading.disabled = true;
    try {
      const payload = createReadingPayload();
      await api("/api/selection", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.readingPayload = payload;
      state.sessionStatus = "waiting_for_model";
      showView("waiting-view");
      startPolling();
    } catch (error) {
      sendReading.disabled = false;
      showToast(error instanceof Error ? error.message : "无法发送牌面。");
    }
  }

  function startPolling() {
    window.clearTimeout(state.pollTimer);
    const poll = async () => {
      try {
        const session = await api("/api/state");
        handleSessionState(session);
      } catch (error) {
        if (!state.resultRendered) {
          showToast(error instanceof Error ? error.message : "暂时无法检查结果。");
        }
      }
      if (
        !state.resultRendered &&
        state.sessionStatus !== "cancelled" &&
        state.sessionStatus !== "expired"
      ) {
        state.pollTimer = window.setTimeout(poll, 900);
      }
    };
    state.pollTimer = window.setTimeout(poll, 250);
  }

  function handleSessionState(session) {
    state.sessionId = session.sessionId || state.sessionId;
    state.sessionStatus = session.status;
    if (session.selection) state.readingPayload = session.selection;
    waitingError.hidden = !session.lastError;

    if (session.status === "completed" && session.interpretation) {
      renderResult(session.interpretation);
      return;
    }
    if (session.status === "waiting_for_model") {
      showView("waiting-view");
      return;
    }
    if (session.status === "cancelled" || session.status === "expired") {
      window.clearTimeout(state.pollTimer);
      showView("terminal-view");
    }
  }

  function renderList(container, values) {
    container.replaceChildren();
    for (const value of values) {
      container.append(element("li", "", value));
    }
  }

  function renderResult(interpretation) {
    state.resultRendered = true;
    state.sessionStatus = "completed";
    window.clearTimeout(state.pollTimer);
    const selection = state.readingPayload;
    document.querySelector("#result-title").textContent = interpretation.headline;
    document.querySelector("#result-summary").textContent = interpretation.summary;

    const meta = document.querySelector("#result-meta");
    meta.replaceChildren();
    if (selection) {
      meta.append(
        element("span", "", TOPIC_LABELS[selection.topic] || selection.topic),
        element("span", "", selection.spread.name),
        element("span", "", `${selection.cards.length} 张牌`),
      );
    }

    const resultCards = document.querySelector("#result-cards");
    resultCards.replaceChildren();
    for (const insight of interpretation.cardInsights) {
      const selectedCard = selection?.cards.find(
        (item) =>
          item.positionId === insight.positionId && item.cardId === insight.cardId,
      );
      const cardData = selectedCard ? cardById(selectedCard.cardId) : null;
      const article = element("article", "result-card");
      const imageWrap = element(
        "div",
        `result-card-image${selectedCard?.orientation === "reversed" ? " is-reversed" : ""}`,
      );
      if (cardData) {
        const image = document.createElement("img");
        image.src = cardData.image;
        image.alt = `${cardData.nameZh}，${ORIENTATION_LABELS[selectedCard.orientation]}`;
        image.width = 480;
        image.height = 720;
        imageWrap.append(image);
      }
      const copy = element("div", "result-card-copy");
      copy.append(
        element(
          "p",
          "",
          selectedCard
            ? `${selectedCard.positionName} · ${ORIENTATION_LABELS[selectedCard.orientation]}`
            : insight.positionId,
        ),
        element("h3", "", insight.title),
        element("p", "", insight.interpretation),
      );
      article.append(imageWrap, copy);
      resultCards.append(article);
    }

    renderList(
      document.querySelector("#result-relationships"),
      interpretation.relationships,
    );
    renderList(document.querySelector("#result-actions"), interpretation.actions);
    renderList(document.querySelector("#result-pauses"), interpretation.pauses);

    const reflections = document.querySelector("#result-reflections");
    reflections.replaceChildren();
    for (const reflection of interpretation.reflections) {
      reflections.append(element("blockquote", "", `“${reflection}”`));
    }
    document.querySelector("#result-boundary").textContent =
      interpretation.boundaryNote;

    [...document.querySelectorAll("#result-view .reveal-section")].forEach(
      (section, index) => {
        section.style.setProperty("--delay", `${Math.min(index * 90, 540)}ms`);
      },
    );
    showView("result-view");
    window.sessionStorage.removeItem(TOKEN_KEY);
    window.setTimeout(() => {
      api("/api/ack", {
        method: "POST",
        body: JSON.stringify({ readingId: interpretation.readingId }),
      }).catch(() => {
        // Result is already rendered. Cleanup also runs on the server timeout path.
      });
    }, 600);
  }

  async function initializeSession() {
    if (!sessionToken) {
      showView("terminal-view");
      document.querySelector("#terminal-title").textContent = "会话链接不完整";
      return;
    }
    try {
      const [data, session] = await Promise.all([
        window.fetch("/data.json", { cache: "no-store" }).then((response) => {
          if (!response.ok) throw new Error("无法读取离线牌组。");
          return response.json();
        }),
        api("/api/state"),
      ]);
      state.data = data;
      state.sessionId = session.sessionId;
      renderSetupOptions();
      if (session.status === "completed" && session.interpretation) {
        state.readingPayload = session.selection;
        renderResult(session.interpretation);
      } else if (session.status === "waiting_for_model" && session.selection) {
        state.readingPayload = session.selection;
        handleSessionState(session);
        startPolling();
      } else if (session.status === "cancelled" || session.status === "expired") {
        handleSessionState(session);
      } else {
        showView("setup-view");
      }
    } catch (error) {
      showView("terminal-view");
      document.querySelector("#terminal-title").textContent = "无法连接本机会话";
      const copy = document.querySelector("#terminal-view > p");
      copy.textContent =
        error instanceof Error
          ? error.message
          : "请从当前对话重新运行月下镜语技能。";
    }
  }

  question.addEventListener("input", () => {
    questionCount.textContent = `${question.value.length} / 200`;
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const topicInput = form.querySelector('input[name="topic"]:checked');
    const spreadInput = form.querySelector('input[name="spread"]:checked');
    state.selectedTopic = topicInput?.value || "general";
    state.selectedSpreadId = spreadInput?.value || "action-three";
    state.question = question.value.trim();
    state.reversalsEnabled = reversals.checked;
    state.deck = secureShuffle(state.data.cards);
    state.selected = [];
    state.selectedDeckIndexes = new Set();
    state.revealed = new Set();
    state.readingPayload = null;
    renderCardWheel();
    renderDrawSlots();
    showView("draw-view");
  });

  autoComplete.addEventListener("click", completeRemainingDraw);
  toReveal.addEventListener("click", () => {
    if (state.selected.length !== currentSpread().positions.length) return;
    renderReveal();
    showView("reveal-view");
  });
  revealAll.addEventListener("click", () => {
    const spread = currentSpread();
    spread.positions.forEach((_, index) => state.revealed.add(index));
    renderReveal();
  });
  sendReading.addEventListener("click", submitReading);
  checkResult.addEventListener("click", async () => {
    checkResult.disabled = true;
    try {
      handleSessionState(await api("/api/state"));
    } catch (error) {
      showToast(error instanceof Error ? error.message : "暂时无法检查结果。");
    } finally {
      checkResult.disabled = false;
    }
  });
  cancelSession.addEventListener("click", async () => {
    cancelSession.disabled = true;
    try {
      await api("/api/cancel", {
        method: "POST",
        body: JSON.stringify({ reason: "user_cancelled" }),
      });
    } catch {
      // The server may finish cleanup before the response reaches the page.
    }
    window.sessionStorage.removeItem(TOKEN_KEY);
    state.sessionStatus = "cancelled";
    showView("terminal-view");
  });

  initializeSession();
})();
