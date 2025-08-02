console.log("🧪 injected.js running inside blob iframe");

window.addEventListener("message", (event) => {
  console.log("📨 Received message in iframe:", event.data);

  if (event.data.action === 'extractRSI') {
    try {
      const studies = window.ChartApiInstance._studyEngine._studiesCache;
      const st4 = studies[4] || studies[7]; // Index based on inspection
      const rsi = st4?._context?._vars?.[7]?.value;
      console.log("📊 RSI Value:", rsi);
      event.source.postMessage({ rsi }, "*");
    } catch (e) {
      console.error("⚠️ Failed to extract RSI:", e);
    }
  }
});
