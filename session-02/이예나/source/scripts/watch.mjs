// test01로 로그인한 쿠키로 1초마다 /api/me와 /api/courses를 요청해, 처리한 APP과 응답을 한 줄씩 찍는다.
// 사용법: node scripts/watch.mjs http://<Public ALB DNS 이름>
const base = process.argv[2]?.replace(/\/$/, "");
if (!base) {
  console.error("사용법: node scripts/watch.mjs http://<Public ALB DNS 이름>");
  process.exit(1);
}

const id = process.env.ID ?? "test01";
const password = process.env.PW ?? "asbg1234";

async function request(path, options = {}) {
  const started = Date.now();
  try {
    const res = await fetch(base + path, { ...options, signal: AbortSignal.timeout(3000) });
    const body = await res.json().catch(() => null);
    return { status: res.status, body, ms: Date.now() - started, cookie: res.headers.get("set-cookie") };
  } catch (err) {
    return { status: err.name === "TimeoutError" ? "timeout" : "error", ms: Date.now() - started };
  }
}

const login = await request("/api/login", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ id, password }),
});
if (login.status !== 200) {
  console.error(`로그인하지 못했습니다: ${login.status}`);
  process.exit(1);
}
const headers = { Cookie: login.cookie.split(";")[0] };

setInterval(async () => {
  const time = new Date().toTimeString().slice(0, 8);
  const [me, courses] = await Promise.all([
    request("/api/me", { headers }),
    request("/api/courses", { headers }),
  ]);
  const app = me.body ? `${me.body.user ?? "-"}  ${me.body.instance} ${me.body.az}` : "-";
  console.log(`${time}  me ${me.status}  ${app}  courses ${courses.status} ${courses.ms}ms`);
}, 1000);
