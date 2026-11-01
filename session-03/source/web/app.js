/* 외부 라이브러리 없이 file:// 또는 로컬 HTTP 서버에서 실행합니다.
 * POST의 응답 시간과 GET으로 확인한 DB 상태를 분리합니다.
 * 입력/실습을 바꾸면 이전 요청과 폴링을 취소하여 결과가 섞이지 않게 합니다.
 */
"use strict";
const $ = (id) => document.getElementById(id);
const POLL_INTERVAL_MS = 1500;
const POLL_LIMIT_MS = 45000; // DLQ 도착을 기다리는 타이머가 아닙니다.
let mode = 1;
let generation = 0;
let timer = null;
let startedAt = 0;
let busy = false;
let lastSnapshot = "";
const controllers = new Set();

function newRegistrationId() {
  const bytes = new Uint8Array(5);
  crypto.getRandomValues(bytes);
  return "reg-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function notice(message = "", isError = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error", isError);
}
function log(message) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = ((performance.now() - startedAt) / 1000).toFixed(1) + "s";
  li.append(time, document.createTextNode(message));
  $("timeline").append(li);
  while ($("timeline").children.length > 30) $("timeline").firstChild.remove();
}
function setBusy(value) {
  busy = value;
  $("registrationForm").querySelectorAll("input,button").forEach((el) => { el.disabled = value; });
  ["refresh", "resend", "step1", "step2", "normalPreset", "failurePreset"].forEach((id) => { $(id).disabled = value; });
  $("submitLabel").textContent = value ? "요청 중…" : "신청하기";
}
function stopPolling() {
  clearTimeout(timer);
  timer = null;
  $("pollStatus").textContent = "";
}
function cancelRequests() {
  generation += 1;
  stopPolling();
  controllers.forEach((controller) => controller.abort());
  controllers.clear();
  setBusy(false);
}
function setStatus(state, label) {
  $("statusPill").dataset.state = state;
  $("statusPill").textContent = label;
}
function resetResult() {
  cancelRequests();
  startedAt = performance.now();
  lastSnapshot = "";
  ["latency", "httpStatus", "issueCount"].forEach((id) => { $(id).textContent = "—"; });
  $("responseJson").textContent = "아직 요청이 없습니다.";
  $("statusJson").textContent = "아직 조회하지 않았습니다.";
  $("timeline").replaceChildren();
  $("resultId").textContent = "신청 번호 —";
  $("badgePreview").dataset.state = "idle";
  $("badgeName").textContent = "아직 신청 전이에요";
  $("badgeOrg").textContent = "신청 후 이곳에서 확인하세요.";
  $("badgeCode").textContent = "AWAITING REGISTRATION";
  setStatus("idle", "대기 중");
  notice();
}
function apiBase() {
  const raw = $("api").value.trim().replace(/\/+$/, "");
  let url;
  try { url = new URL(raw); } catch { throw new Error("배포 후 출력된 API 주소를 입력하세요."); }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(local && url.protocol === "http:")) || url.username || url.password || url.search || url.hash) {
    throw new Error("https://로 시작하는 API 주소를 입력하세요. 쿼리와 인증 정보는 제외하세요.");
  }
  return raw;
}
function saveApi() {
  const base = apiBase();
  $("api").value = base;
  $("apiError").hidden = true;
  $("connectionStatus").textContent = "주소 적용됨";
  $("connectionDot").classList.add("ready");
  try { localStorage.setItem("apiUrl", base); } catch { /* 파일 저장소가 제한되어도 사용 가능 */ }
  return base;
}
function showApiError(error) {
  $("connection").open = true;
  $("apiError").textContent = error.message;
  $("apiError").hidden = false;
  $("api").focus();
}
function requestContext() {
  let base;
  try { base = saveApi(); } catch (error) { showApiError(error); return null; }
  const id = $("rid").value.trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) {
    $("rid").reportValidity();
    notice("신청 번호를 확인하세요.", true);
    return null;
  }
  return { base, id, generation };
}
function current(ctx) { return generation === ctx.generation; }
async function request(ctx, method, path, payload) {
  const controller = new AbortController();
  controllers.add(controller);
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 35000);
  const start = performance.now();
  try {
    const options = { method, signal: controller.signal, cache: "no-store" };
    if (payload !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(ctx.base + path, options);
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = null; }
    const object = data !== null && typeof data === "object" && !Array.isArray(data);
    if (response.ok) {
      const replay = path.endsWith("/resend");
      if (!object || data.registrationId !== ctx.id ||
          (!replay && !["RECEIVED", "PENDING", "ISSUED"].includes(data.status))) {
        throw new Error("API 응답 형식이 올바르지 않습니다. 배포 주소와 실습 코드를 확인하세요.");
      }
    } else if (!object) {
      data = { message: raw.slice(0, 300) || "응답 본문이 없습니다." };
    }
    return { status: response.status, data, elapsed: performance.now() - start };
  } catch (error) {
    if (timedOut) throw new Error("응답 대기 시간이 지났습니다. 상태 조회로 저장 여부를 확인하세요.");
    throw error;
  } finally {
    clearTimeout(timeout);
    controllers.delete(controller);
  }
}
function showResponse(result, ctx) {
  $("latency").textContent = (result.elapsed / 1000).toFixed(2);
  const unit = document.createElement("small");
  unit.textContent = "초";
  $("latency").append(unit);
  $("httpStatus").textContent = result.status;
  $("responseJson").textContent = JSON.stringify(result.data, null, 2);
  $("resultId").textContent = ctx.id;
  log("HTTP " + result.status + " · " + (result.elapsed / 1000).toFixed(2) + "초");
}
function showItem(item, ctx) {
  $("statusJson").textContent = JSON.stringify(item, null, 2);
  $("resultId").textContent = ctx.id;
  $("issueCount").textContent = item.issueCount === undefined ? "—" : item.issueCount;
  const issued = item.status === "ISSUED";
  setStatus(issued ? "issued" : "pending", issued ? "발급 완료" : item.status === "RECEIVED" ? "신청 저장됨" : "발급 대기");
  $("badgePreview").dataset.state = issued ? "issued" : "pending";
  $("badgeName").textContent = issued ? item.name : "신청이 접수됐어요";
  $("badgeOrg").textContent = issued ? (item.organization || "소속 없음") : "참가증 발급을 기다리고 있어요.";
  $("badgeCode").textContent = issued ? item.badgeCode : item.status;
  const snapshot = JSON.stringify([item.status, item.badgeCode, item.issueCount]);
  if (lastSnapshot !== snapshot) {
    log("DB · " + item.status + (item.issueCount !== undefined ? " · 발급 " + item.issueCount + "회" : ""));
    lastSnapshot = snapshot;
  }
}
function pending(ctx) {
  $("resultId").textContent = ctx.id;
  setStatus("pending", "발급 대기");
  $("badgePreview").dataset.state = "pending";
  $("badgeName").textContent = "신청이 접수됐어요";
  $("badgeOrg").textContent = "참가증 발급을 기다리고 있어요.";
  $("badgeCode").textContent = "PENDING";
}
function errorNotice(error, ctx) {
  if (!current(ctx) || error.name === "AbortError") return;
  notice(error.message === "Failed to fetch" || error instanceof TypeError ? "연결하지 못했어요. API 주소와 CORS 설정을 확인하세요." : error.message, true);
  log("요청을 완료하지 못했습니다.");
}
async function checkStatus(ctx) {
  const result = await request(ctx, "GET", "/registrations/" + encodeURIComponent(ctx.id));
  if (!current(ctx)) return { stop: true };
  if (result.status !== 200) {
    $("statusJson").textContent = JSON.stringify(result.data, null, 2);
    setStatus("error", "조회 확인 필요");
    notice("상태 조회 HTTP " + result.status + " · " + (result.data.message || "조회할 수 없습니다."), true);
    log("상태 조회 · HTTP " + result.status);
    return { stop: true };
  }
  showItem(result.data, ctx);
  return { issued: result.data.status === "ISSUED" };
}
function startPolling(ctx, observeReplay = false) {
  stopPolling();
  const deadline = performance.now() + (observeReplay ? 15000 : POLL_LIMIT_MS);
  $("pollStatus").textContent = "자동 조회 중";
  const poll = async () => {
    if (!current(ctx)) return;
    try {
      const state = await checkStatus(ctx);
      if (!current(ctx)) return;
      if (state.stop || (state.issued && !observeReplay)) { stopPolling(); return; }
      if (performance.now() >= deadline) {
        stopPolling();
        notice(observeReplay ? "재전송 관찰을 마쳤어요. 필요하면 상태 조회를 눌러주세요." : "자동 조회를 마쳤어요. Lambda 로그와 DLQ를 확인한 뒤 상태 조회를 눌러주세요.");
        log("자동 조회 종료");
        return;
      }
      // 직전 GET이 끝난 뒤 다음 요청을 예약하여 중첩 폴링을 방지합니다.
      timer = setTimeout(poll, POLL_INTERVAL_MS);
    } catch (error) {
      if (current(ctx)) { stopPolling(); errorNotice(error, ctx); }
    }
  };
  timer = setTimeout(poll, POLL_INTERVAL_MS);
}
async function submit(event) {
  event.preventDefault();
  if (busy) return;
  const validated = requestContext();
  if (!validated) return;
  const payload = { registrationId: validated.id, name: $("name").value, email: $("email").value, organization: $("org").value };
  resetResult();
  const ctx = { ...validated, generation };
  setBusy(true);
  setStatus("pending", "요청 중");
  $("resultId").textContent = ctx.id;
  log("신청 전송 · " + ctx.id);
  try {
    const result = await request(ctx, "POST", "/registrations", payload);
    if (!current(ctx)) return;
    showResponse(result, ctx);
    if ([200, 201, 202].includes(result.status)) {
      if (result.data.status === "ISSUED") await checkStatus(ctx);
      else { pending(ctx); startPolling(ctx); }
      if (result.status === 200) notice("이미 접수된 신청이에요. 새 작업은 만들지 않았습니다.");
    } else {
      setStatus("error", "요청 확인 필요");
      notice(result.data.message || "요청을 처리하지 못했습니다.", true);
      if (result.status === 503 || result.status >= 500) await checkStatus(ctx);
    }
  } catch (error) {
    if (current(ctx)) { setStatus("error", "응답 확인 필요"); errorNotice(error, ctx); }
  } finally { if (current(ctx)) setBusy(false); }
}
async function refresh() {
  if (busy) return;
  const validated = requestContext();
  if (!validated) return;
  cancelRequests();
  const ctx = { ...validated, generation };
  if (!startedAt) startedAt = performance.now();
  setBusy(true);
  notice();
  try { await checkStatus(ctx); } catch (error) { errorNotice(error, ctx); }
  finally { if (current(ctx)) setBusy(false); }
}
async function resend() {
  if (mode !== 2 || busy) return;
  const validated = requestContext();
  if (!validated) return;
  cancelRequests();
  const ctx = { ...validated, generation };
  setBusy(true);
  notice();
  log("작업 재전송 · " + ctx.id);
  try {
    const result = await request(ctx, "POST", "/registrations/" + encodeURIComponent(ctx.id) + "/resend");
    if (!current(ctx)) return;
    showResponse(result, ctx);
    if (result.status === 202) startPolling(ctx, true);
    else notice(result.data.message || "재전송하지 못했습니다.", true);
  } catch (error) { errorNotice(error, ctx); }
  finally { if (current(ctx)) setBusy(false); }
}
function selectMode(next) {
  mode = next;
  resetResult();
  $("rid").value = newRegistrationId();
  $("org").value = "ASBG";
  [1, 2].forEach((value) => {
    $("step" + value).classList.toggle("active", mode === value);
    $("step" + value).setAttribute("aria-pressed", String(mode === value));
  });
  $("presets").hidden = mode !== 2;
  $("resend").hidden = mode !== 2;
  $("modeNote").textContent = "화면 선택은 배포된 AWS 구성을 바꾸지 않습니다.";
  syncPreset();
}
function syncPreset() {
  const empty = !$("org").value.trim();
  $("normalPreset").classList.toggle("selected", !empty);
  $("failurePreset").classList.toggle("selected", empty);
  $("normalPreset").setAttribute("aria-pressed", String(!empty));
  $("failurePreset").setAttribute("aria-pressed", String(empty));
}
function preset(failure) {
  resetResult();
  $("rid").value = newRegistrationId();
  $("org").value = failure ? "" : "ASBG";
  syncPreset();
  if (failure) notice("소속을 비웠어요. 신청하기를 눌러 오류 실험을 시작하세요.");
}
$("registrationForm").addEventListener("submit", submit);
$("refresh").addEventListener("click", refresh);
$("resend").addEventListener("click", resend);
$("newId").addEventListener("click", () => { resetResult(); $("rid").value = newRegistrationId(); });
$("rid").addEventListener("input", resetResult);
$("org").addEventListener("input", syncPreset);
$("step1").addEventListener("click", () => selectMode(1));
$("step2").addEventListener("click", () => selectMode(2));
$("normalPreset").addEventListener("click", () => preset(false));
$("failurePreset").addEventListener("click", () => preset(true));
$("api").addEventListener("input", () => {
  resetResult();
  $("connectionDot").classList.remove("ready");
  $("connectionStatus").textContent = "변경한 주소를 적용하세요";
});
$("apiForm").addEventListener("submit", (event) => {
  event.preventDefault();
  try { saveApi(); resetResult(); $("connection").open = false; }
  catch (error) { showApiError(error); }
});
try { $("api").value = localStorage.getItem("apiUrl") || ""; } catch { /* 저장소 없이 동작 */ }
if ($("api").value) { try { saveApi(); $("connection").open = false; } catch { /* 새 주소 입력 */ } }
$("rid").value = newRegistrationId();
resetResult();
