// background.js (MV3 service worker)

// --------------------
// Helper functions
// --------------------
function isMemePage(url) {
  return url?.startsWith("https://axiom.trade/meme/");
}

// Retry sending startPolling until content script is ready
function sendStartPolling(tabId, retries = 5) {
  if (retries <= 0) return console.warn("❌ startPolling failed after retries");

  chrome.tabs.sendMessage(tabId, { type: "startPolling" }, (resp) => {
    if (chrome.runtime.lastError) {
      // Retry after 300ms
      setTimeout(() => sendStartPolling(tabId, retries - 1), 300);
    }
  });
}

// Promisified getAllFrames
function getAllFrames(tabId) {
  return new Promise((resolve) => {
    chrome.webNavigation.getAllFrames({ tabId }, (frames) => {
      resolve(frames || []);
    });
  });
}

// --------------------
// Tab update listener
// --------------------
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && isMemePage(tab.url)) {
    // Try sending startPolling first
    chrome.tabs.sendMessage(tabId, { type: "startPolling" }, (resp) => {
      if (chrome.runtime.lastError) {
        // If content.js not loaded, inject and retry
        chrome.scripting.executeScript(
          { target: { tabId }, files: ["content.js"] },
          () => sendStartPolling(tabId)
        );
      }
    });
  }
});

// --------------------
// Runtime message handler
// --------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!sender.tab) return;

  const tabId = sender.tab.id;

  // --------------------
  // Inject script into blob iframe
  // --------------------
  if (msg?.type === "inject_injected_js") {
    const { iframeSrc, token } = msg;

    getAllFrames(tabId).then((frames) => {
      const targetFrame = frames.find(f => f.url === iframeSrc);

      if (!targetFrame) {
        console.warn("❌ No frame matches blob iframe src:", iframeSrc, "frames:", frames);
        sendResponse({ status: "error", reason: "frame not found" });
        return;
      }

      const frameId = targetFrame.frameId;

      // Inject injected.js into the exact frame
      chrome.scripting.executeScript(
        { target: { tabId, frameIds: [frameId] }, files: ["injected.js"] },
        () => {
          // Send token info after injection
          chrome.scripting.executeScript({
            target: { tabId, frameIds: [frameId] },
            func: (token) => {
              window.postMessage({ type: "tokenInfo", token }, "*");
            },
            args: [token]
          }, () => sendResponse({ status: "ok" }));
        }
      );
    });

    return true; // keep sendResponse alive
  }

  // --------------------
  // Forward candle data to local server
  // --------------------
  if (msg?.type === "candles") {
    const payload = { candles: msg.payload, token: msg.token, initial: msg.initial };

    fetch("http://localhost:5000/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
    .then(() => {
      console.log("✅ [BG] Candles sent for", msg.token?.name);
      sendResponse({ status: "ok" });
    })
    .catch(err => {
      console.warn("❌ [BG] Failed to send:", err);
      sendResponse({ status: "error", error: err.toString() });
    });

    return true; // keep sendResponse alive
  }
});
