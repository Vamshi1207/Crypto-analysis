document.getElementById('setRegion').addEventListener('click', () => {
  chrome.tabs.query({active: true, currentWindow: true}, (tabs) => {
    chrome.scripting.executeScript({
      target: {tabId: tabs[0].id},
      files: ['setRegion.js'],
      world: 'ISOLATED'
    });
  });
});

document.getElementById('capture').addEventListener('click', () => {
  console.log("[Popup] Capture button clicked.");
  chrome.runtime.sendMessage({ action: 'captureIndicators' });
});
