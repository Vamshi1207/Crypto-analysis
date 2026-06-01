console.log("🚀 content.js started");

var pollingStarted = false;

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "startPolling" && !pollingStarted) {
    pollingStarted = true;
    setupIframeInjection();
  }
});

function setupIframeInjection() {
  const observer = new MutationObserver(() => {
    const iframe = document.querySelector('iframe[src^="blob:"]');
    if (!iframe || iframe.dataset.injected) return;

    iframe.dataset.injected = "true";
    console.log("🧬 Blob iframe detected:", iframe.src);

    iframe.addEventListener("load", () => {
      console.log("📩 Injecting script into blob iframe");

      const tokenAddress = window.location.pathname.split('/').pop();
      let tokenName = "Unknown";

      const tokenContainers = document.querySelectorAll('span.text-textPrimary span > div');
      for (const div of tokenContainers) {
        const text = div.innerText.trim();
        if (text && /^[a-zA-Z0-9!]+$/.test(text)) {
          tokenName = text;
          break;
        }
      }

      console.log("✅ Token name:", tokenName);

      // Inject external script (CSP-safe)
      const script = document.createElement("script");
      script.src = chrome.runtime.getURL('injected.js'); // chrome-extension://<your-id>/injected.js
      script.async = false;

      script.onload = () => {
      console.log("[inject] injected.js loaded via extension URL");

      const sendTokenInfo = () => {
        try {
          iframe.contentWindow.postMessage(
            { type: "tokenInfo", address: tokenAddress, name: tokenName },
            "*"
          );
          console.log("📤 Sent tokenInfo");
        } catch (e) {
          console.warn("[inject] postMessage failed", e);
        }
      };

      // 🔥 NEW: listen for injected.js ready signal
      const readyListener = (event) => {
        if (event.data?.type === "injectedReady") {
          console.log("✅ injected.js ready");

          window.removeEventListener("message", readyListener);
          sendTokenInfo();
        }
      };

      window.addEventListener("message", readyListener);

      // 🔥 fallback (in case ready message is missed)
      setTimeout(() => {
        console.warn("⚠️ Fallback token send");
        sendTokenInfo();
      }, 1000);
    };

      script.onerror = (e) => {
        console.warn("[inject] extension script load failed", e);
      };

      try {
        const head = iframe.contentWindow.document.head || iframe.contentWindow.document.documentElement;
        head.appendChild(script);
      } catch (e) {
        console.warn("Cannot access iframe document:", e);
      }
    });
  });

  observer.observe(document, { childList: true, subtree: true });

  // Listen for messages from iframe and forward to background
  window.addEventListener("message", (event) => {
    if (event.data?.type === "candles") {
      console.log("📨 [CONTENT] Forwarding candles message", {
        id: event.data.id,
        token: event.data.token?.name,
        initial: event.data.initial,
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
    }
  });
}
