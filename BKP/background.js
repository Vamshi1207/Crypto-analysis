const POLL_INTERVAL = 1000;

function isMemePage(url) {
  return url?.startsWith("https://axiom.trade/meme/");
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (isMemePage(tab.url)) {
    chrome.tabs.sendMessage(tabId, { type: "startPolling" });
  }
});

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type === "candles") {
    fetch("http://localhost:5000/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candles: msg.payload, token: msg.token })
    }).then(() => {
      console.log("✅ [BG] Candles sent for", msg.token?.name);
    }).catch(err => {
      console.warn("❌ [BG] Failed to send:", err);
    });
  }
});
