console.log("📦 Script injected in blob (live stream mode)");

if (window.__axiomInjectedRunnerActive) {
  console.warn("⚠️ injected.js already active in this iframe, skipping duplicate bootstrap");
} else {
  window.__axiomInjectedRunnerActive = true;

try {
  window.parent.postMessage({ type: "injectedReady" }, "*");
} catch (_) {
  /* ignore */
}

let token = { address: "unknown", name: "unknown" };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const encoder = new TextEncoder();
const MAX_MESSAGE_BYTES = 8 * 1024 * 1024;

// Live monitoring: seed a short recent window, then stream forming bars.
// Historical full-history dump was the old dataset-collection path.
const LIVE_MODE = true;
const LIVE_FLUSH_MS = 500;
const LIVE_SEED_BARS = {
  "5S": 360,   // 30 min
  "15S": 240,  // 60 min
  "30S": 240,  // 2 h
  "1": 256,    // ~4 h
  "3": 200,
  "5": 180,
  "15": 120,
  "30": 96,
  "60": 72
};

window.addEventListener("message", (event) => {
  if (event.data?.type === "tokenInfo") {
    token = {
      address: event.data.address ?? event.data.token?.address ?? "unknown",
      name: event.data.name ?? event.data.token?.name ?? "unknown"
    };
    console.log("💡 Token info received:", token);
  }
});

const waitForToken = () => {
  return new Promise((resolve, reject) => {
    let waited = 0;

    const interval = setInterval(() => {
      if (token.address && token.address !== "unknown") {
        clearInterval(interval);
        resolve(token.address);
      }

      waited += 100;
      if (waited > 5000) {
        clearInterval(interval);
        reject("Token not received in time");
      }
    }, 100);
  });
};

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
  console.log("🔍 Available resolve keys:", Object.keys(engine._resolveRequests || {}));
  const mint = await waitForToken();
  console.log("✅ Using token:", mint);

  const resolveKey = Object.keys(engine._resolveRequests || {}).find((key) =>
    key.toUpperCase().startsWith(mint.toUpperCase())
  );

  const symbolInfo = engine._resolveRequests[resolveKey];

  if (!symbolInfo) {
    console.error("❌ Could not resolve symbol info for", token.name, mint);
    return;
  }

  const resolutions = ["5S", "15S", "30S", "1", "3", "5", "15", "30", "60"];

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

  const payloadId = Date.now() + "_" + Math.floor(Math.random() * 10000);
  console.log("🆔 Live session started", { payloadId, token, LIVE_MODE });

  let initialChunkSent = false;
  const sendToParent = (data, isInitial = false, options = {}) => {
    window.parent.postMessage(
      {
        id: payloadId,
        type: "candles",
        token,
        payload: data,
        initial: isInitial,
        live: true,
        complete: Boolean(options.complete)
      },
      "*"
    );
  };

  const sendChunkToParent = (data) => {
    const isInitial = !initialChunkSent;
    sendToParent(data, isInitial);
    initialChunkSent = true;
  };

  const getJsonByteSize = (value) => encoder.encode(JSON.stringify(value)).length;

  const chunkObjectEntriesBySize = (entries, maxBytes) => {
    const chunks = [];
    let currentChunk = {};
    let currentSize = 2;

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

  const sendCandleEntriesInChunks = async (res, candleEntries, label = "seed") => {
    const candleChunks = chunkObjectEntriesBySize(candleEntries, MAX_MESSAGE_BYTES);
    if (!candleChunks.length) return 0;

    let totalBarsSent = 0;
    for (let index = 0; index < candleChunks.length; index += 1) {
      const candleChunk = candleChunks[index];
      const barCount = Object.keys(candleChunk).length;
      totalBarsSent += barCount;
      sendChunkToParent({ candles: { [res]: candleChunk }, stats: [] });
      console.log(
        `✅ [${payloadId}] ${res} ${label} chunk ${index + 1}/${candleChunks.length} (${barCount} bars)`
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
        { from, to, countBack, firstDataRequest },
        (bars) => {
          settled = true;
          resolve({ bars: Array.isArray(bars) ? bars : [], status: "ok" });
        },
        (error) => {
          console.error("❌ Failed to fetch bars for", res, ":", error);
          settled = true;
          resolve({ bars: [], status: "error", error });
        }
      );

      setTimeout(() => {
        if (settled) return;
        console.warn(`⏳ ${res} page fetch timed out`, { from, to, countBack });
        resolve({ bars: [], status: "timeout" });
      }, 15000);
    });

  const seedRecentBarsForResolution = async (res) => {
    const countBack = LIVE_SEED_BARS[res] || 128;
    const now = Math.floor(Date.now() / 1000);
    const from = Math.max(0, now - secondsPerBar[res] * countBack - 60);
    const pageResult = await fetchBarsPage(res, from, now, countBack, true);
    const bars = pageResult.bars;
    if (!bars.length) {
      console.warn(`⚠️ [${payloadId}] ${res} seed empty (${pageResult.status})`);
      return 0;
    }

    // Keep only the most recent countBack bars.
    const trimmed = bars.slice(-countBack);
    const pageEntries = trimmed.map((bar) => [
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
    const sent = await sendCandleEntriesInChunks(res, pageEntries, "seed");
    console.log(`✅ [${payloadId}] ${res} seeded ${sent} recent bars`);
    return sent;
  };

  const seedAllResolutions = async () => {
    for (const res of resolutions) {
      await seedRecentBarsForResolution(res);
      await sleep(40);
    }
  };

  // Pending live bar updates keyed by resolution -> timestamp -> candle
  const pendingLiveBars = {};

  function startSubscriptions() {
    resolutions.forEach((res) => {
      datafeed.subscribeBars(
        symbolInfo,
        res,
        (bar) => {
          if (!pendingLiveBars[res]) pendingLiveBars[res] = {};
          // Upsert forming bar by timestamp so OHLC updates within the same
          // bar are sent, not only the first tick of a new bar.
          pendingLiveBars[res][bar.time] = {
            timestamp: bar.time,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
            timeMs: bar.timeMs
          };
        },
        "listener_live_" + res + "_" + payloadId
      );
    });

    setInterval(() => {
      const resolutionsWithUpdates = Object.keys(pendingLiveBars);
      if (!resolutionsWithUpdates.length) return;

      const candles = {};
      for (const res of resolutionsWithUpdates) {
        const map = pendingLiveBars[res];
        if (!map || !Object.keys(map).length) continue;
        candles[res] = map;
        delete pendingLiveBars[res];
      }

      if (!Object.keys(candles).length) return;
      sendToParent({ candles, stats: [] }, false);
    }, LIVE_FLUSH_MS);

    console.log(`📡 [${payloadId}] Live subscriptions active for`, resolutions.join(", "));
  }

  try {
    console.log(`🌱 [${payloadId}] Seeding recent OHLCV…`);
    await seedAllResolutions();
    console.log(`✅ [${payloadId}] Seed complete — starting live stream`);
    startSubscriptions();
  } catch (err) {
    console.error(`❌ [${payloadId}] Live bootstrap failed:`, err);
  }
})();
}
