// background.js (MV3 service worker)

// --------------------
// Helper functions
// --------------------
const WS_URL = "ws://localhost:8080/ws";
let ws;
let wsConnected = false;
let wsReconnectTimer = null;
const wsQueue = [];

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  ws = new WebSocket(WS_URL);

  ws.addEventListener("open", () => {
    wsConnected = true;
    flushWebSocketQueue();
    console.log("✅ [BG] WebSocket connected");
  });

  ws.addEventListener("close", () => {
    wsConnected = false;
    console.warn("⚠️ [BG] WebSocket closed, retrying...");
    scheduleWebSocketReconnect();
  });

  ws.addEventListener("error", (err) => {
    wsConnected = false;
    console.warn("❌ [BG] WebSocket error:", err);
    try {
      ws.close();
    } catch (closeErr) {
      console.warn("❌ [BG] WebSocket close error:", closeErr);
    }
  });

  ws.addEventListener("message", (event) => {
    console.log("📨 [BG] WebSocket message:", event.data);
  });
}

function scheduleWebSocketReconnect() {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWebSocket();
  }, 1000);
}

function flushWebSocketQueue() {
  while (wsQueue.length && wsConnected) {
    ws.send(wsQueue.shift());
  }
}

function sendToServer(payload) {
  const message = JSON.stringify(payload);
  if (wsConnected) {
    ws.send(message);
    return;
  }

  wsQueue.push(message);
  connectWebSocket();
}

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
  connectWebSocket();

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
    const payload = {
      id: msg.id,
      candles: msg.payload,
      token: msg.token,
      initial: msg.initial
    };

    try {
      sendToServer(payload);
      console.log("✅ [BG] Candles queued for", msg.token?.name);
      sendResponse({ status: "queued" });
    } catch (err) {
      console.warn("❌ [BG] Failed to queue:", err);
      sendResponse({ status: "error", error: err.toString() });
    }

    return true; // keep sendResponse alive
  }
});

connectWebSocket();
