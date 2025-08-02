console.log("🚀 content.js started");

let pollingStarted = false;

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
    const nameContainer = document.querySelector('div.flex.flex-row.gap-\\[4px\\].justify-start.items-center');
	const tokenNameEl = nameContainer?.querySelector('span');
	const tokenName = tokenNameEl ? tokenNameEl.innerText.trim() : 'Unknown';



    iframe.contentWindow.postMessage({
      type: "tokenInfo",
      address: tokenAddress,
      name: tokenName
    }, "*");  

      const js = `
        console.log("📦 Script injected in blob");
		
		let token = { address: "unknown", name: "unknown" };

        window.addEventListener("message", (event) => {
          if (event.data?.type === "tokenInfo") {
            token = event.data;
            console.log("💡 Token info received:", token);
          }
        });

        let attempts = 0;
        const interval = setInterval(() => {
          attempts++;
          try {
            const instance = window.ChartApiInstance;
            if (!instance) {
              console.log("⏳ [", attempts, "] ChartApiInstance not ready");
              return;
            }

            const engine = instance._studyEngine;
            if (!engine) {
              console.log("⏳ [", attempts, "] studyEngine missing");
              return;
            }

            const rawObjects = engine?._objectsDataCache;
            const key = rawObjects ? Object.keys(rawObjects)[0] : null;
            const candles = key ? rawObjects[key] : [];

            if (!candles.length) {
              console.log("⏳ [", attempts, "] No candle data yet");
              return;
            }

            const payload = candles.map(c => ({
              timestamp: c.value?.[0],  // Unix timestamp
              open: c.value?.[1],
              high: c.value?.[2],
              low: c.value?.[3],
              close: c.value?.[4],
              volume: c.value?.[5],
              timeMs: c.timeMs
            }));
			
		
			window.parent.postMessage({
									  type: "candles",
									  payload,
									  token
									}, "*");
            console.log("📤 Sent", payload.length, "candles to main window");

          } catch (err) {
            console.warn("❌ Error getting chart data:", err);
          }
        }, 1000);
      `;

      const script = document.createElement("script");
      script.textContent = js;
      iframe.contentWindow.document.documentElement.appendChild(script);
    });
  });

  observer.observe(document, { childList: true, subtree: true });

  // Listen for messages from iframe and forward to background
  window.addEventListener("message", (event) => {
    if (event.data?.type === "candles") {
      chrome.runtime.sendMessage(event.data);
    }
  });

  // Inject token info to iframe
  const iframe = document.querySelector('iframe[src^="blob:"]');
  if (iframe) {
    const tokenAddress = window.location.pathname.split('/').pop();
    const tokenNameEl = document.querySelector('div.flex.flex-row span.text-textPrimary');
    const tokenName = tokenNameEl ? tokenNameEl.innerText.trim() : 'Unknown';
    iframe.contentWindow.postMessage({ type: "tokenInfo", address: tokenAddress, name: tokenName }, "*");
  }
}

// Listen for message from blob iframe and send to Python server
window.addEventListener("message", (event) => {
  if (event.data?.type === "candles") {
    const payload = {
      candles: event.data.payload,
      token: event.data.token || { address: "unknown", name: "unknown" }
    };

    console.log("📨 Sending payload:", payload);

    fetch("http://localhost:5000/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(res => {
      console.log("✅ Sent candle data to server");
    }).catch(err => {
      console.error("❌ Failed to send candle data to server:", err);
    });
  }
});