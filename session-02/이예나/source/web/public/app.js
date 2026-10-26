const $ = (id) => document.getElementById(id);
const served = { web: "-", app: "-" };

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  served.web = res.headers.get("X-Web-Instance") || served.web;
  served.app = res.headers.get("X-App-Instance") || served.app;
  $("served").textContent = `처리 서버  WEB ${served.web}  ·  APP ${served.app}`;
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  return { status: res.status, data };
}

function showLogin(message = "") {
  $("login-msg").textContent = message;
  $("login").hidden = false;
  $("courses").hidden = true;
  $("account").hidden = true;
}

function showCourses(me) {
  $("who").textContent = `${me.name} (${me.user})`;
  $("login").hidden = true;
  $("courses").hidden = false;
  $("account").hidden = false;
  $("msg").textContent = "";
  loadCourses();
}

function notify(text, error = false) {
  $("msg").textContent = text;
  $("msg").className = error ? "msg error" : "msg";
}

async function loadCourses() {
  const { status, data } = await api("GET", "/api/courses");
  if (status !== 200) return notify("강좌 목록을 불러오지 못했습니다.", true);
  $("rows").replaceChildren(...data.map(row));
  const mine = data.filter((c) => c.mine);
  const credits = mine.reduce((sum, c) => sum + c.credits, 0);
  $("summary").textContent = `신청 ${mine.length}과목 · ${credits}학점`;
}

function row(course) {
  const tr = document.createElement("tr");
  if (course.mine) tr.className = "mine";
  const cells = [
    [course.id],
    [course.title],
    [course.professor],
    [course.credits, "num"],
    [course.schedule],
    [`${course.enrolled}/${course.capacity}`, "num"],
  ];
  for (const [text, className] of cells) {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    tr.append(td);
  }

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  if (course.mine) {
    button.textContent = "취소";
    button.onclick = () => cancel(course);
  } else if (course.enrolled >= course.capacity) {
    button.textContent = "마감";
    button.disabled = true;
  } else {
    button.textContent = "신청";
    button.classList.add("accent");
    button.onclick = () => enroll(course);
  }
  const td = document.createElement("td");
  td.append(button);
  tr.append(td);
  return tr;
}

async function enroll(course) {
  const { status, data } = await api("POST", "/api/enrollments", { courseId: course.id });
  if (status === 401) return showLogin("로그인이 풀렸습니다. 다시 로그인해 주세요.");
  if (status === 201) notify(`${course.title} 신청이 완료되었습니다.`);
  else notify(data?.error || "신청하지 못했습니다.", true);
  loadCourses();
}

async function cancel(course) {
  if (!confirm(`${course.title} 신청을 취소할까요?`)) return;
  const { status, data } = await api("DELETE", `/api/enrollments/${encodeURIComponent(course.id)}`);
  if (status === 401) return showLogin("로그인이 풀렸습니다. 다시 로그인해 주세요.");
  if (status === 204) notify(`${course.title} 신청을 취소했습니다.`);
  else notify(data?.error || "취소하지 못했습니다.", true);
  loadCourses();
}

$("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const { status, data } = await api("POST", "/api/login", {
    id: form.get("id"),
    password: form.get("password"),
  });
  if (status !== 200) return showLogin(data?.error || "로그인하지 못했습니다.");
  event.target.reset();
  showCourses(data);
});

$("logout").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  showLogin();
});

$("reload").addEventListener("click", () => {
  notify("");
  loadCourses();
});

api("GET", "/api/me").then(({ status, data }) => {
  if (status === 200) showCourses(data);
  else showLogin();
});
