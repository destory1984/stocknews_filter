// content.js 가 읽은 표를 종목 뉴스 필터(127.0.0.1:18766/mb)로 보낸다.
// 페이지 안에서 바로 보내면 MarketBeat 페이지의 보안 정책에 막히므로 확장 뒤편에서 보낸다.
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || msg.type !== "mb") return;
  fetch("http://127.0.0.1:18766/mb", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Settings": "yes" },
    body: JSON.stringify({ rows: msg.rows, refreshed: msg.refreshed }),
  })
    .then((r) => (r.ok ? r.json() : { ok: false, msg: "HTTP " + r.status }))
    .then(reply)
    .catch((e) => reply({ ok: false, msg: "알리미가 꺼져 있음 (" + e.message + ")" }));
  return true;   // 비동기로 답한다
});
