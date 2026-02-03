chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'cropImage') {
    console.log("[ContentScript] Received screenshot for cropping.");

    chrome.storage.local.get('cropRegion', (result) => {
      if (!result.cropRegion) {
        alert('No crop region defined. Please set it first!');
        return;
      }

      const { x, y, width, height } = result.cropRegion;
      cropImage(request.dataUrl, x, y, width, height).then((croppedDataUrl) => {
        sendResponse({ croppedDataUrl });
      });
    });

    return true;
  }
});

function cropImage(dataUrl, cropX, cropY, cropWidth, cropHeight) {
  return new Promise((resolve) => {
    const img = new Image();
    img.src = dataUrl;

    img.onload = () => {
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');

      canvas.width = cropWidth;
      canvas.height = cropHeight;

      ctx.drawImage(img, cropX, cropY, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);

      resolve(canvas.toDataURL('image/png'));
    };

    img.onerror = (err) => {
      console.error("[ContentScript] Error loading image:", err);
      resolve(null);
    };
  });
}
