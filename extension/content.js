console.log("🚀 content.js started");

// Declared with all_frames:true — only the top page owns the TradingView blob iframe.
if (window !== window.top) {
  // no-op in nested frames
} else {
  let pollingStarted = false;

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "startPolling" && !pollingStarted) {
      pollingStarted = true;
      setupIframeInjection();
    }
  });

  // Don't rely solely on background startPolling (race on SPA navigations).
  setupIframeInjection();
}

function setupIframeInjection() {
  if (window.__axiomIframeObserverActive) return;
  window.__axiomIframeObserverActive = true;

  const scan = () => {
    document.querySelectorAll('iframe[src^="blob:"]').forEach((iframe) => {
      maybeInject(iframe);
    });
  };

  const observer = new MutationObserver(scan);
  observer.observe(document.documentElement || document, {
    childList: true,
    subtree: true
  });
  scan();

  // Forward candle payloads from the page/iframe up to the background WS bridge.
  window.addEventListener("message", (event) => {
    if (event.data?.type !== "candles") return;

    console.log("📨 [CONTENT] Forwarding candles message", {
      id: event.data.id,
      token: event.data.token?.name,
      initial: event.data.initial,
      live: event.data.live === true,
      complete: event.data.complete === true
    });

    try {
      chrome.runtime.sendMessage(event.data, (response) => {
        if (chrome.runtime.lastError) {
          console.warn("❌ [CONTENT] sendMessage failed", chrome.runtime.lastError.message);
          return;
        }
        console.log("✅ [CONTENT] Background ack", response);
      });
    } catch (error) {
      console.warn("❌ [CONTENT] sendMessage threw", error);
    }
  });
}

function getTokenMeta() {
  const tokenAddress = window.location.pathname.split("/").pop();
  let tokenName = "Unknown";
  const tokenContainers = document.querySelectorAll("span.text-textPrimary span > div");
  for (const div of tokenContainers) {
    const text = (div.innerText || "").trim();
    if (text && /^[a-zA-Z0-9!]+$/.test(text)) {
      tokenName = text;
      break;
    }
  }
  return { tokenAddress, tokenName };
}

function maybeInject(iframe) {
  if (iframe.dataset.axiomInjectState === "done" || iframe.dataset.axiomInjectState === "pending") {
    return;
  }
  iframe.dataset.axiomInjectState = "pending";
  console.log("🧬 Blob iframe detected:", iframe.src);

  const attempt = () => injectScriptTag(iframe);

  // Race fix: blob iframes are often already loaded before we attach a listener.
  iframe.addEventListener(
    "load",
    () => {
      console.log("📩 iframe load event — injecting");
      attempt();
    },
    { once: true }
  );

  // Immediate + short poll if load already fired.
  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    if (iframe.dataset.axiomInjectState === "done") {
      clearInterval(timer);
      return;
    }
    if (attempt() || tries >= 40) {
      clearInterval(timer);
      if (iframe.dataset.axiomInjectState !== "done") {
        console.warn("⚠️ Gave up injecting into blob iframe after retries");
        iframe.dataset.axiomInjectState = "";
      }
    }
  }, 250);
}

function injectScriptTag(iframe) {
  if (iframe.dataset.axiomInjectState === "done") return true;

  let doc;
  try {
    doc = iframe.contentDocument || iframe.contentWindow?.document;
  } catch (e) {
    console.warn("Cannot access iframe document yet:", e);
    return false;
  }
  if (!doc || !doc.documentElement) return false;

  // Already present from a prior attempt in this document.
  if (doc.documentElement.dataset.axiomInjected === "1") {
    iframe.dataset.axiomInjectState = "done";
    return true;
  }

  const { tokenAddress, tokenName } = getTokenMeta();
  console.log("📩 Injecting script into blob iframe", { tokenAddress, tokenName });

  const script = doc.createElement("script");
  script.src = chrome.runtime.getURL("injected.js");
  script.async = false;

  const sendTokenInfo = () => {
    try {
      iframe.contentWindow.postMessage(
        { type: "tokenInfo", address: tokenAddress, name: tokenName },
        "*"
      );
      console.log("📤 Sent tokenInfo", { address: tokenAddress, name: tokenName });
    } catch (e) {
      console.warn("[inject] postMessage failed", e);
    }
  };

  script.onload = () => {
    console.log("[inject] injected.js loaded via extension URL");
    iframe.dataset.axiomInjectState = "done";
    doc.documentElement.dataset.axiomInjected = "1";

    const readyListener = (event) => {
      if (event.data?.type === "injectedReady") {
        console.log("✅ injected.js ready");
        window.removeEventListener("message", readyListener);
        sendTokenInfo();
      }
    };
    window.addEventListener("message", readyListener);
    setTimeout(() => {
      console.warn("⚠️ Fallback token send");
      sendTokenInfo();
    }, 1000);
  };

  script.onerror = (e) => {
    console.warn("[inject] extension script load failed — trying text injection", e);
    // CSP fallback: fetch extension script and eval via textContent (same-origin blob doc).
    fetch(chrome.runtime.getURL("injected.js"))
      .then((r) => r.text())
      .then((code) => {
        const inline = doc.createElement("script");
        inline.textContent = code;
        (doc.head || doc.documentElement).appendChild(inline);
        iframe.dataset.axiomInjectState = "done";
        doc.documentElement.dataset.axiomInjected = "1";
        console.log("[inject] injected.js loaded via inline text");
        sendTokenInfo();
      })
      .catch((err) => {
        console.error("[inject] inline fallback failed", err);
        iframe.dataset.axiomInjectState = "";
      });
  };

  try {
    (doc.head || doc.documentElement).appendChild(script);
    return false; // wait for onload
  } catch (e) {
    console.warn("Cannot append script to iframe:", e);
    iframe.dataset.axiomInjectState = "";
    return false;
  }
}
