chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'captureIndicators') {
    console.log("[ServiceWorker] Received 'captureIndicators'");
    startCaptureFlow();
  }
});

function startCaptureFlow() {
  console.log("[ServiceWorker] Starting capture flow...");

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tabId = tabs[0].id;

    chrome.storage.local.get('cropRegion', (result) => {
      const region = result.cropRegion;
      if (!region) {
        console.error("[ServiceWorker] No crop region defined!");
        return;
      }

      console.log("[ServiceWorker] Loaded region:", region);

      chrome.tabs.captureVisibleTab(null, { format: "png" }, (dataUrl) => {
        console.log("[ServiceWorker] Screenshot captured!");

        // 🔥 Ensure content.js is injected
        chrome.scripting.executeScript({
          target: { tabId },
          files: ['content.js']
        }, () => {
          console.log("[ServiceWorker] content.js injected!");

          chrome.tabs.sendMessage(tabId, { 
            action: 'cropImage', 
            dataUrl, 
            region 
          }, (response) => {
            if (chrome.runtime.lastError) {
              console.error("[ServiceWorker] Error sending message:", chrome.runtime.lastError.message);
              return;
            }

            console.log("[ServiceWorker] Cropped image received!");
            sendToServer(response.croppedDataUrl);
          });
        });
      });
    });
  });
}


function sendToServer(base64Image) {
  console.log("[ServiceWorker] Sending cropped image to server...");

  fetch('http://localhost:5000/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ image: base64Image.split(',')[1] })
  })
  .then(res => res.json())
  .then(data => {
    console.log("[ServiceWorker] Server responded:", data);
  })
  .catch(err => {
    console.error("[ServiceWorker] Error sending to server:", err);
  });
}
