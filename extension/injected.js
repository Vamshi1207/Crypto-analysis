console.log("📦 Script injected in blob");

let token = { address: "unknown", name: "unknown" };

window.addEventListener("message", (event) => {
  if (event.data?.type === "tokenInfo") {
    token = {
      address: event.data.address,
      name: event.data.name
    };
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
	"1S": 18000,    // 18,000 bars (1s each)
	"5S": 3600,     // 18,000 / 5
	"15S": 1200,    // 18,000 / 15
	"30S": 600,     // 18,000 / 30
	"1": 300,       // 18,000 / 60
	"3": 100,       // 18,000 / 180
	"5": 60         // 18,000 / 300
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
