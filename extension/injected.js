console.log("📦 Script injected in blob");

if (window.__axiomInjectedRunnerActive) {
  console.warn("⚠️ injected.js already active in this iframe, skipping duplicate bootstrap");
} else {
  window.__axiomInjectedRunnerActive = true;

let token = { address: "unknown", name: "unknown" };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const encoder = new TextEncoder();
const MAX_MESSAGE_BYTES = 8 * 1024 * 1024;

window.addEventListener("message", (event) => {
  if (event.data?.type === "tokenInfo") {
    token = {
      address: event.data.address ?? event.data.token?.address ?? "unknown",
      name: event.data.name ?? event.data.token?.name ?? "unknown"
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

  const mint = token.address;

  // Find the key that starts with your mint address (case-insensitive)
  const resolveKey = Object.keys(engine._resolveRequests).find(key => 
    key.toUpperCase().startsWith(mint.toUpperCase())
  );

  const symbolInfo = engine._resolveRequests[resolveKey];

  if (!symbolInfo) {
    console.error("❌ Could not resolve symbol info for", token.name, mint);
    return;
  }


  const resolutions = [
	"5S", "15S", "30S",
	"1", "3", "5",
	"15", "30", "60"
	];

  const secondsPerBar = {
	"5S": 5,
	"15S": 15,
	"30S": 30,

	"1": 60,
	"3": 180,
	"5": 300,
	"15": 900,
	"30": 1800,
	"60": 3600
	};
  const historyPageBars = {
	"5S": 17280,
	"15S": 5760,
	"30S": 2880,

	"1": 1440,
	"3": 480,
	"5": 288,
	"15": 96,
	"30": 48,
	"60": 24
	};

  const payloadId = Date.now() + "_" + Math.floor(Math.random() * 10000);
  console.log("🆔 Upload session started", { payloadId, token });
  let initialChunkSent = false;
  const sendToParent = (data, isInitial = false, options = {}) => {
	window.parent.postMessage({
	  id: payloadId,	
	  type: "candles",
	  token,
	  payload: data,
	  initial: isInitial,
	  complete: Boolean(options.complete)
	}, "*");
  };
  const sendChunkToParent = (data) => {
	const isInitial = !initialChunkSent;
	sendToParent(data, isInitial);
	initialChunkSent = true;
  };
  const sendFinalizeToParent = () => {
	sendToParent({ candles: {}, stats: [] }, false, { complete: true });
  };

  const getJsonByteSize = (value) => encoder.encode(JSON.stringify(value)).length;

  const chunkObjectEntriesBySize = (entries, maxBytes) => {
	const chunks = [];
	let currentChunk = {};
	let currentSize = 2; // {}

	for (const [key, value] of entries) {
	  const entrySize = getJsonByteSize({ [key]: value });

	  if (Object.keys(currentChunk).length > 0 && currentSize + entrySize > maxBytes) {
		chunks.push(currentChunk);
		currentChunk = {};
		currentSize = 2;
	  }

	  currentChunk[key] = value;
	  currentSize += entrySize;
	}

	if (Object.keys(currentChunk).length > 0) {
	  chunks.push(currentChunk);
	}

	return chunks;
  };

  const chunkArrayBySize = (items, maxBytes) => {
	const chunks = [];
	let currentChunk = [];
	let currentSize = 2; // []

	for (const item of items) {
	  const itemSize = getJsonByteSize(item);

	  if (currentChunk.length > 0 && currentSize + itemSize > maxBytes) {
		chunks.push(currentChunk);
		currentChunk = [];
		currentSize = 2;
	  }

	  currentChunk.push(item);
	  currentSize += itemSize;
	}

	if (currentChunk.length > 0) {
	  chunks.push(currentChunk);
	}

	return chunks;
  };

  const sendInitialPayloadInChunks = async () => {
	const statsChunks = chunkArrayBySize(initialStats, MAX_MESSAGE_BYTES);
	if (!statsChunks.length) {
	  sendChunkToParent({ candles: {}, stats: [] });
	  console.log(`✅ [${payloadId}] Initial empty stats payload sent`);
	} else {
	  for (let index = 0; index < statsChunks.length; index += 1) {
		sendChunkToParent({ candles: {}, stats: statsChunks[index] });
		console.log(
		  `✅ [${payloadId}] Initial stats chunk ${index + 1}/${statsChunks.length} sent (${statsChunks[index].length} buckets)`
		);
		await sleep(50);
	  }
	}
  };

  const sendCandleEntriesInChunks = async (res, candleEntries, pageIndex = 1) => {
	const candleChunks = chunkObjectEntriesBySize(candleEntries, MAX_MESSAGE_BYTES);

	if (!candleChunks.length) {
	  return 0;
	}

	let totalBarsSent = 0;
	for (let index = 0; index < candleChunks.length; index += 1) {
	  const candleChunk = candleChunks[index];
	  const barCount = Object.keys(candleChunk).length;
	  totalBarsSent += barCount;
	  sendChunkToParent({ candles: { [res]: candleChunk }, stats: [] });
	  console.log(
		`✅ [${payloadId}] ${res} page ${pageIndex} chunk ${index + 1}/${candleChunks.length} sent (${barCount} bars)`
	  );
	  await sleep(25);
	}

	return totalBarsSent;
  };

  const fetchBarsPage = (res, from, to, countBack, firstDataRequest) =>
	new Promise((resolve) => {
	  let settled = false;
	  datafeed.getBars(
		symbolInfo,
		res,
		{
		  from,
		  to,
		  countBack,
		  firstDataRequest
		},
		(bars) => {
		  settled = true;
		  resolve({
			bars: Array.isArray(bars) ? bars : [],
			status: "ok"
		  });
		},
		(error) => {
		  console.error("❌ Failed to fetch bars for", res, ":", error);
		  settled = true;
		  resolve({
			bars: [],
			status: "error",
			error
		  });
		}
	  );

	  setTimeout(() => {
		if (settled) return;
		console.warn(`⏳ ${res} page fetch timed out`, { from, to, countBack });
		resolve({
		  bars: [],
		  status: "timeout"
		});
	  }, 15000);
	});

  const fetchAndSendBarsForResolution = async (res) => {
	const now = Math.floor(Date.now() / 1000);
	const pageBars = historyPageBars[res];
	const pageSeconds = secondsPerBar[res] * pageBars;
	let to = now;
	let firstRequest = true;
	let pageCount = 0;
	let stopReason = "unknown";
	let totalBarsSent = 0;

	while (true) {
	  const from = Math.max(0, to - pageSeconds);
	  const pageResult = await fetchBarsPage(res, from, to, pageBars, firstRequest);
	  const bars = pageResult.bars;
	  firstRequest = false;
	  pageCount += 1;

	  if (!bars.length) {
		if (pageResult.status === "error") {
		  stopReason = "upstream_error";
		} else if (pageResult.status === "timeout") {
		  stopReason = "timeout";
		} else {
		  stopReason = "empty_page";
		}
		break;
	  }

	  const pageEntries = bars.map((bar) => [
		bar.time,
		{
		  timestamp: bar.time,
		  open: bar.open,
		  high: bar.high,
		  low: bar.low,
		  close: bar.close,
		  volume: bar.volume,
		  timeMs: bar.timeMs
		}
	  ]);
	  const sentThisPage = await sendCandleEntriesInChunks(res, pageEntries, pageCount);
	  totalBarsSent += sentThisPage;
	  console.log(
		`✅ [${payloadId}] ${res} cumulative sent after page ${pageCount}: ${totalBarsSent} bars`
	  );

	  const oldestBarTime = Math.min(...bars.map((bar) => bar.time));
	  const oldestBarTimeSec = Math.floor(oldestBarTime / 1000);

	  if (sentThisPage === 0 || oldestBarTimeSec <= 0) {
		stopReason = sentThisPage === 0 ? "no_new_bars" : "reached_epoch";
		break;
	  }

	  if (bars.length < pageBars) {
		stopReason = "short_page_end_of_history";
		break;
	  }

	  to = oldestBarTimeSec - 1;
	}

	console.log(
	  `✅ [${payloadId}] ${res} history sent: ${totalBarsSent} bars across ${pageCount} page(s); stop=${stopReason}`
	);
  };
  const fetchAndSendAllBars = async () => {
	for (const res of resolutions) {
	  await fetchAndSendBarsForResolution(res);
	  await sleep(50);
	}
  };

  const fetchInitialStats = async () => {
	if (!mint || mint === "unknown") {
	  console.warn("⚠️ Skipping stats fetch, token address missing");
	  return;
	}

	try {
	  const response = await fetch(
		`https://api3.axiom.trade/pair-stats?pairAddress=${encodeURIComponent(mint)}`,
		{ credentials: "include" }
	  );

	  if (!response.ok) {
		throw new Error(`HTTP ${response.status}`);
	  }

	  const stats = await response.json();
	  if (!Array.isArray(stats)) {
		console.warn("⚠️ pair-stats response is not an array", stats);
		return;
	  }

	  initialStats = stats;
	  console.log(`✅ [${payloadId}] Initial stats fetched. Buckets count:`, stats.length);
	} catch (error) {
	  console.error("❌ Failed to fetch initial stats:", error);
	}
  };

  let initialStats = [];

  await fetchInitialStats();
  await sendInitialPayloadInChunks();
  console.log(`✅ [${payloadId}] Initial stats sent`);
  await fetchAndSendAllBars();
  console.log(`✅ [${payloadId}] Initial payload sent`);
  sendFinalizeToParent();
  console.log(`🏁 [${payloadId}] Finalize signal sent`);
  //startSubscriptions();

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
  sendToParent({ candles: { ...latestBars } });
  for (const key in latestBars) delete latestBars[key];
}, 500);
}
})();
}
