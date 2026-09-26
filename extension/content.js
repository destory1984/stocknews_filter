// MarketBeat "오늘의 목표가 변경" 표를 읽어 종목 뉴스 필터로 넘긴다.
// 사용자가 브라우저에 띄워 둔 탭에서만 돈다. 5분마다 페이지를 새로 고친다.
const RELOAD_MIN = 5;

function cellClean(td) {
  return (td && (td.getAttribute("data-clean") || td.getAttribute("data-sort-value") || td.textContent) || "").trim();
}

function readRows() {
  const table = document.querySelector("table.scroll-table.sort-table");
  if (!table) return [];
  const rows = [];
  for (const tr of table.querySelectorAll("tbody tr")) {
    const td = tr.querySelectorAll("td");
    if (td.length < 8) continue;
    const link = td[7].getAttribute("data-clean") || "";
    const id = (link.match(/details\/(\d+)/) || [])[1];
    if (!id) continue;
    const [ticker, company] = cellClean(td[0]).split("|");
    const [ptOld, ptNew] = cellClean(td[5]).split("|");
    const [ratingOld, ratingNew] = cellClean(td[6]).split("|");
    rows.push({
      id, ticker: ticker || "", company: company || "",
      action: cellClean(td[1]),
      brokerage: cellClean(td[2]).split("|")[0],
      analyst: cellClean(td[3]).split("|")[0],
      price: cellClean(td[4]).split("|")[0],
      pt_old: ptOld === "0" ? "" : (ptOld || ""), pt_new: ptNew === "0" ? "" : (ptNew || ""),
      rating_old: ratingOld || "", rating_new: ratingNew || "",
    });
  }
  return rows;
}

function refreshedText() {
  const m = document.body.innerText.match(/last refreshed on ([^.]+)\./i);
  return m ? m[1].trim() : "";
}

function badge(text, ok) {
  let el = document.getElementById("stocknews-mb-badge");
  if (!el) {
    el = document.createElement("div");
    el.id = "stocknews-mb-badge";
    el.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:99999;padding:6px 10px;border-radius:8px;" +
      "font:12px system-ui,sans-serif;color:#fff;box-shadow:0 2px 8px rgba(0,0,0,.3)";
    document.body.appendChild(el);
  }
  el.style.background = ok ? "#1f6f43" : "#8a2c2c";
  el.textContent = text;
}

async function run() {
  const rows = readRows();
  const now = new Date().toLocaleTimeString("ko-KR", { hour12: false });
  if (!rows.length) {
    badge(`종목 뉴스 필터 측정: 표를 찾지 못함 (${now})`, false);
  } else {
    try {
      const res = await chrome.runtime.sendMessage({ type: "mb", rows, refreshed: refreshedText() });
      if (res && res.ok) badge(`종목 뉴스 필터 측정: ${rows.length}줄, 새 줄 ${res.new} (${now}) · ${RELOAD_MIN}분마다 새로 고침`, true);
      else badge(`종목 뉴스 필터에 넘기지 못함: ${(res && res.msg) || "응답 없음"} (${now})`, false);
    } catch (e) {
      badge(`종목 뉴스 필터에 넘기지 못함: ${e.message} (${now})`, false);
    }
  }
  // 조금씩 흩어 새로 고친다 (5분 ± 20초)
  setTimeout(() => location.reload(), RELOAD_MIN * 60000 + (Math.random() - 0.5) * 40000);
}

run();
