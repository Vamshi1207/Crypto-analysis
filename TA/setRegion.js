(() => {
  console.log("[SetRegion] Starting crop region selector");

  // ✅ Remove any existing overlay if present
  const existing = document.getElementById('my-crop-overlay');
  if (existing) {
    existing.remove();
    console.log("[SetRegion] Removed existing overlay to prevent duplicates");
  }

  // ✅ Create overlay
  const overlay = document.createElement('div');
  overlay.id = 'my-crop-overlay';
  overlay.style.position = 'fixed';
  overlay.style.top = '0';
  overlay.style.left = '0';
  overlay.style.width = '100%';
  overlay.style.height = '100%';
  overlay.style.backgroundColor = 'rgba(0,0,0,0.3)';
  overlay.style.zIndex = '999999';
  overlay.style.cursor = 'crosshair';
  document.body.appendChild(overlay);

  // ✅ Create selection box
  const box = document.createElement('div');
  box.id = 'my-crop-box';
  box.style.position = 'absolute';
  box.style.top = '100px';
  box.style.left = '100px';
  box.style.width = '300px';
  box.style.height = '200px';
  box.style.border = '2px dashed #fff';
  box.style.backgroundColor = 'rgba(255,255,255,0.2)';
  box.style.boxSizing = 'border-box';
  overlay.appendChild(box);

  // ✅ Add resize handles
  const handles = ['nw','ne','sw','se'];
  handles.forEach(pos => {
    const handle = document.createElement('div');
    handle.className = 'resize-handle ' + pos;
    handle.style.position = 'absolute';
    handle.style.width = '12px';
    handle.style.height = '12px';
    handle.style.background = '#fff';
    handle.style.border = '1px solid #000';
    handle.style.zIndex = '1000000';
    handle.style.cursor = pos + '-resize';
    switch (pos) {
      case 'nw':
        handle.style.top = '-6px'; handle.style.left = '-6px'; break;
      case 'ne':
        handle.style.top = '-6px'; handle.style.right = '-6px'; break;
      case 'sw':
        handle.style.bottom = '-6px'; handle.style.left = '-6px'; break;
      case 'se':
        handle.style.bottom = '-6px'; handle.style.right = '-6px'; break;
    }
    box.appendChild(handle);
  });

  // ✅ Dragging
  let isDragging = false;
  let dragOffsetX, dragOffsetY;
  box.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('resize-handle')) return;
    isDragging = true;
    dragOffsetX = e.clientX - box.offsetLeft;
    dragOffsetY = e.clientY - box.offsetTop;
    e.preventDefault();
  });

  overlay.addEventListener('mousemove', (e) => {
    if (isDragging) {
      box.style.left = (e.clientX - dragOffsetX) + 'px';
      box.style.top = (e.clientY - dragOffsetY) + 'px';
    }
  });

  overlay.addEventListener('mouseup', () => {
    isDragging = false;
    isResizing = false;
  });

  // ✅ Resizing
  let isResizing = false;
  let currentHandle = null;
  let startX, startY, startWidth, startHeight, startLeft, startTop;

  box.querySelectorAll('.resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', (e) => {
      isResizing = true;
      currentHandle = handle.classList.contains('nw') ? 'nw' :
                      handle.classList.contains('ne') ? 'ne' :
                      handle.classList.contains('sw') ? 'sw' :
                      'se';
      startX = e.clientX;
      startY = e.clientY;
      startWidth = parseInt(document.defaultView.getComputedStyle(box).width, 10);
      startHeight = parseInt(document.defaultView.getComputedStyle(box).height, 10);
      startLeft = box.offsetLeft;
      startTop = box.offsetTop;
      e.preventDefault();
      e.stopPropagation();
    });
  });

  overlay.addEventListener('mousemove', (e) => {
    if (!isResizing) return;
    let dx = e.clientX - startX;
    let dy = e.clientY - startY;

    if (currentHandle === 'se') {
      box.style.width = (startWidth + dx) + 'px';
      box.style.height = (startHeight + dy) + 'px';
    }
    if (currentHandle === 'sw') {
      box.style.width = (startWidth - dx) + 'px';
      box.style.height = (startHeight + dy) + 'px';
      box.style.left = (startLeft + dx) + 'px';
    }
    if (currentHandle === 'ne') {
      box.style.width = (startWidth + dx) + 'px';
      box.style.height = (startHeight - dy) + 'px';
      box.style.top = (startTop + dy) + 'px';
    }
    if (currentHandle === 'nw') {
      box.style.width = (startWidth - dx) + 'px';
      box.style.height = (startHeight - dy) + 'px';
      box.style.left = (startLeft + dx) + 'px';
      box.style.top = (startTop + dy) + 'px';
    }
  });

  // ✅ Create Save Button
  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save Region';
  saveBtn.style.position = 'fixed';
  saveBtn.style.bottom = '20px';
  saveBtn.style.right = '20px';
  saveBtn.style.padding = '10px';
  saveBtn.style.background = '#4CAF50';
  saveBtn.style.color = '#fff';
  saveBtn.style.border = 'none';
  saveBtn.style.cursor = 'pointer';
  saveBtn.style.zIndex = '1000000';
  overlay.appendChild(saveBtn);

  saveBtn.addEventListener('click', () => {
    const rect = {
      x: parseInt(box.style.left, 10),
      y: parseInt(box.style.top, 10),
      width: parseInt(box.style.width, 10),
      height: parseInt(box.style.height, 10)
    };
    console.log("[SetRegion] Saving region:", rect);
    chrome.storage.local.set({cropRegion: rect}, () => {
      alert('Crop region saved!');
      overlay.remove();
    });
  });
})();
