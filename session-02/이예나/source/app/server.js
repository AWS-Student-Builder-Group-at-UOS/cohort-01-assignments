import os from "node:os";
import express from "express";
import session from "express-session";
import { RedisStore } from "connect-redis";
import { createCluster } from "redis";
import { GetSecretValueCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { HttpError, checkPassword, connectDatabase, db, transaction } from "./db.js";

const env = process.env;
const PORT = Number(env.PORT || 8080);
const secrets = new SecretsManagerClient({});

async function readSecret(id) {
  const { SecretString } = await secrets.send(new GetSecretValueCommand({ SecretId: id }));
  return JSON.parse(SecretString);
}

async function dbCredentials() {
  if (env.DB_USER) return { user: env.DB_USER, password: env.DB_PASSWORD };
  const { username, password } = await readSecret(env.DB_SECRET_ID);
  return { user: username, password };
}

async function sessionSecret() {
  if (env.SESSION_SECRET) return env.SESSION_SECRET;
  return (await readSecret(env.SESSION_SECRET_ID)).secret;
}

async function whoami() {
  const imds = "http://169.254.169.254/latest";
  try {
    const token = await fetch(`${imds}/api/token`, {
      method: "PUT",
      headers: { "X-aws-ec2-metadata-token-ttl-seconds": "60" },
      signal: AbortSignal.timeout(1000),
    }).then((res) => res.text());
    const meta = async (path) => {
      const res = await fetch(`${imds}/meta-data/${path}`, {
        headers: { "X-aws-ec2-metadata-token": token },
        signal: AbortSignal.timeout(1000),
      });
      if (!res.ok) throw new Error(`IMDS ${res.status}`);
      return res.text();
    };
    return { instance: await meta("instance-id"), az: await meta("placement/availability-zone") };
  } catch {
    return { instance: `${os.hostname()}:${PORT}`, az: "local" };
  }
}

function sessionStore() {
  const url = new URL(env.REDIS_URL);
  const client = createCluster({
    rootNodes: [{ url: env.REDIS_URL }],
    defaults: { socket: { tls: url.protocol === "rediss:" } },
    // 서버리스 캐시는 클러스터 모드로만 접속된다. 노드 주소는 엔드포인트 이름으로 맞춰야 TLS 인증서와 일치한다.
    nodeAddressMap: (address) => ({ host: url.hostname, port: Number(address.split(":").pop()) }),
  });
  client.on("error", (err) => console.error("redis:", err.message || err.name));
  return client;
}

const server = await whoami();
await connectDatabase(dbCredentials);
const redis = sessionStore();
await redis.connect();

function profile(req) {
  const user = req.session.user;
  return { user: user?.id ?? null, name: user?.name ?? null, ...server };
}

function requireLogin(req, res, next) {
  if (!req.session.user) throw new HttpError(401, "로그인이 필요합니다.");
  next();
}

const api = express.Router();

api.get("/me", (req, res) => {
  res.status(req.session.user ? 200 : 401).json(profile(req));
});

api.post("/login", async (req, res) => {
  const id = String(req.body?.id ?? "").trim();
  const password = String(req.body?.password ?? "");
  const [[student]] = await db((pool) =>
    pool.query("SELECT id, name, password_hash FROM students WHERE id = ?", [id]),
  );
  if (!student || !(await checkPassword(password, student.password_hash))) {
    throw new HttpError(401, "학번 또는 비밀번호가 맞지 않습니다.");
  }
  await new Promise((resolve, reject) => {
    req.session.regenerate((err) => (err ? reject(err) : resolve()));
  });
  req.session.user = { id: student.id, name: student.name };
  res.json(profile(req));
});

api.post("/logout", (req, res, next) => {
  req.session.destroy((err) => {
    if (err) return next(err);
    res.clearCookie("sid").status(204).end();
  });
});

api.get("/courses", async (req, res) => {
  const [rows] = await db((pool) =>
    pool.query(
      `SELECT c.id, c.title, c.professor, c.credits, c.schedule, c.capacity, c.enrolled,
              e.student_id IS NOT NULL AS mine
         FROM courses c
         LEFT JOIN enrollments e ON e.course_id = c.id AND e.student_id = ?
        ORDER BY c.id`,
      [req.session.user?.id ?? ""],
    ),
  );
  res.json(rows.map((row) => ({ ...row, mine: row.mine === 1 })));
});

// 자리 확인과 기록을 한 트랜잭션에서 처리해 APP이 몇 대든 정원을 넘지 않게 한다.
api.post("/enrollments", requireLogin, async (req, res) => {
  const courseId = String(req.body?.courseId ?? "");
  await transaction(async (conn) => {
    const [seat] = await conn.query(
      "UPDATE courses SET enrolled = enrolled + 1 WHERE id = ? AND enrolled < capacity",
      [courseId],
    );
    if (seat.affectedRows === 0) {
      const [[course]] = await conn.query("SELECT id FROM courses WHERE id = ?", [courseId]);
      throw course ? new HttpError(409, "정원이 찼습니다.") : new HttpError(404, "없는 과목입니다.");
    }
    try {
      await conn.query("INSERT INTO enrollments (student_id, course_id) VALUES (?, ?)", [
        req.session.user.id,
        courseId,
      ]);
    } catch (err) {
      if (err.code === "ER_DUP_ENTRY") throw new HttpError(409, "이미 신청한 과목입니다.");
      throw err;
    }
  });
  res.status(201).json({ courseId });
});

api.delete("/enrollments/:courseId", requireLogin, async (req, res) => {
  const { courseId } = req.params;
  await transaction(async (conn) => {
    const [removed] = await conn.query(
      "DELETE FROM enrollments WHERE student_id = ? AND course_id = ?",
      [req.session.user.id, courseId],
    );
    if (removed.affectedRows === 0) throw new HttpError(404, "신청 내역이 없습니다.");
    await conn.query("UPDATE courses SET enrolled = enrolled - 1 WHERE id = ?", [courseId]);
  });
  res.status(204).end();
});

const app = express();
app.disable("x-powered-by");
app.set("trust proxy", true);

app.use((req, res, next) => {
  res.set("X-App-Instance", `${server.instance} ${server.az}`);
  next();
});

app.get("/health", (req, res) => {
  res.type("text").send("ok\n");
});

app.use(
  "/api",
  express.json(),
  session({
    name: "sid",
    store: new RedisStore({ client: redis }),
    secret: await sessionSecret(),
    resave: false,
    saveUninitialized: false,
    rolling: true,
    cookie: { httpOnly: true, sameSite: "lax", maxAge: 30 * 60 * 1000 },
  }),
  api,
);

app.use((err, req, res, next) => {
  if (err instanceof HttpError) return res.status(err.status).json({ error: err.message });
  if (err.status === 400) return res.status(400).json({ error: "요청 형식이 맞지 않습니다." });
  console.error(err);
  res.status(500).json({ error: "잠시 후 다시 시도해 주세요." });
});

const listener = app.listen(PORT, () => {
  console.log(`portal-app listening on ${PORT} (${server.instance} ${server.az})`);
});

process.on("SIGTERM", () => {
  listener.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
});
