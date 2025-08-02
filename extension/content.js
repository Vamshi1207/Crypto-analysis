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
	const allPrimarySpans = Array.from(document.querySelectorAll('span.text-textPrimary'));
	let tokenName = 'Unknown';

	for (const span of allPrimarySpans) {
	const text = span.innerText.trim();
	// Skip if it's empty or just a symbol
	if (text && text !== '/' && text.length > 2 && /^[a-zA-Z0-9\s]+$/.test(text)) {
		tokenName = text;
		break;
	}
	}

	console.log("✅ Token name:", tokenName);

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

			(async () => {
			  const waitForChart = () => {
				return new Promise((resolve) => {
				  const interval = setInterval(() => {
					const instance = window.ChartApiInstance;
					const engine = instance?._studyEngine;
					const datafeed = engine?._externalDatafeed;
					if (instance && engine && datafeed) {
					  clearInterval(interval);
					  resolve({ instance, engine, datafeed });
					}
				  }, 500);
				});
			  };

			  const { engine, datafeed } = await waitForChart();
			  console.log("🔍 Available resolve keys:", Object.keys(engine._resolveRequests));

			  const symbolInfo = await engine._resolveRequests[token.name.toUpperCase() ];
				if (!symbolInfo) {
				  console.error("❌ Could not resolve symbol info for", token.name);
				  return;
				}

			  const resolutions = ["1S", "5S", "15S", "30S", "1", "3", "5"];
			  const secondsPerBar = {
				"1S": 1, "5S": 5, "15S": 15, "30S": 30,
				"1": 60, "3": 180, "5": 300
			  };
			  const desiredBars = {
				"1S": 300, "5S": 60, "15S": 20, "30S": 10,
				"1": 5, "3": 1, "5": 1
			  };

			  const initialPayload = {};
			  let completed = 0;
			  const payloadId = Date.now() + "_" + Math.floor(Math.random() * 10000);
			  const sendToParent = (data, isInitial = false) => {
				window.parent.postMessage({
				  id: payloadId,	
				  type: "candles",
				  token,
				  payload: data,
				  initial: isInitial
				}, "*");
			  };

			  // Fetch initial bars for all resolutions
			  resolutions.forEach((res) => {
				const now = Math.floor(Date.now() / 1000);
				const from = now - secondsPerBar[res] * desiredBars[res];
				const to = now;

				datafeed.getBars(
				  symbolInfo,
				  res,
				  {
					from,
					to,
					countBack: desiredBars[res],
					firstDataRequest: true
				  },
				  (bars) => {
					  if (bars && bars.length > 0) {
						const barMap = {};
						bars.forEach(bar => {
						  barMap[bar.time] = {
							timestamp: bar.time,
							open: bar.open,
							high: bar.high,
							low: bar.low,
							close: bar.close,
							volume: bar.volume,
							timeMs: bar.timeMs
						  };
						});
						initialPayload[res] = barMap;
					  }

					completed++;
					if (completed === resolutions.length) {
					  console.log("✅ All initial bars fetched");
					  sendToParent(initialPayload, true);
					  console.log("✅ Initial payload sent");
					  startSubscriptions();
					}
				  },
				  (error) => {
					console.error("❌ Failed to fetch bars for", res, ":", error);
					completed++;
				  }
				);
			  });

			    const latestBars = {};
				const lastBarTime = {};  // Track last sent bar time per resolution

				function startSubscriptions() {
				resolutions.forEach((res) => {
					datafeed.subscribeBars(
					symbolInfo,
					res,
					(bar) => {
						// Only update if the bar is new
						if (bar.time !== lastBarTime[res]) {
						const barMap = {};
						barMap[bar.time] = {
							timestamp: bar.time,
							open: bar.open,
							high: bar.high,
							low: bar.low,
							close: bar.close,
							volume: bar.volume,
							timeMs: bar.timeMs
						};
						latestBars[res] = barMap;
						lastBarTime[res] = bar.time;
						}
					},
					"listener_" + res
					);
				});


				  setInterval(() => {
              if (Object.keys(latestBars).length === 0) return;
              sendToParent({ ...latestBars });
              for (const key in latestBars) delete latestBars[key];
            }, 500);
          }
        })();
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
	  //id: Date.now() + "_" + Math.floor(Math.random() * 10000),
	  id: event.data.id,
      candles: event.data.payload,
      token: event.data.token || { address: "unknown", name: "unknown" },
	  initial: event.data.initial
    };

    console.log("📨 Sending payload:", payload);

    fetch("http://localhost:5000/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(res => {
	  console.log("✅ Sent candle data to server (ID:", payload.id, ")");	
      //console.log("✅ Sent candle data to server");
    }).catch(err => {
      console.error("❌ Failed to send candle data to server:", err);
    });
  }
});