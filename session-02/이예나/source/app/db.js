import { randomBytes, scrypt as scryptCallback, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";
import awsCaBundle from "aws-ssl-profiles";
import mysql from "mysql2/promise";
import { COURSES, STUDENTS, TEST_PASSWORD } from "./seed.js";

const env = process.env;
const scrypt = promisify(scryptCallback);
const DB_NAME = "portal";

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS students (
     id VARCHAR(20) PRIMARY KEY,
     name VARCHAR(40) NOT NULL,
     password_hash VARCHAR(200) NOT NULL
   )`,
  `CREATE TABLE IF NOT EXISTS courses (
     id VARCHAR(20) PRIMARY KEY,
     title VARCHAR(60) NOT NULL,
     professor VARCHAR(40) NOT NULL,
     credits TINYINT NOT NULL,
     schedule VARCHAR(40) NOT NULL,
     capacity INT NOT NULL,
     enrolled INT NOT NULL DEFAULT 0
   )`,
  `CREATE TABLE IF NOT EXISTS enrollments (
     student_id VARCHAR(20) NOT NULL,
     course_id VARCHAR(20) NOT NULL,
     created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
     PRIMARY KEY (student_id, course_id)
   )`,
];

let pool;
let loadCredentials;
let reopening = null;

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function connectionOptions({ user, password }) {
  return {
    host: env.DB_HOST,
    port: Number(env.DB_PORT || 3306),
    user,
    password,
    ssl: env.DB_SSL === "off" ? undefined : { ca: awsCaBundle.ca },
  };
}

async function hashPassword(password) {
  const salt = randomBytes(16);
  const key = await scrypt(password, salt, 32);
  return `${salt.toString("hex")}:${key.toString("hex")}`;
}

export async function checkPassword(password, stored) {
  const [salt, key] = stored.split(":");
  const actual = await scrypt(password, Buffer.from(salt, "hex"), 32);
  return timingSafeEqual(actual, Buffer.from(key, "hex"));
}

async function prepare(credentials) {
  const conn = await mysql.createConnection(connectionOptions(credentials));
  try {
    await conn.query(`CREATE DATABASE IF NOT EXISTS ${DB_NAME}`);
    await conn.query(`USE ${DB_NAME}`);
    for (const sql of SCHEMA) await conn.query(sql);
    await conn.query(
      "INSERT IGNORE INTO courses (id, title, professor, credits, schedule, capacity) VALUES ?",
      [COURSES],
    );
    const [[{ count }]] = await conn.query("SELECT COUNT(*) AS count FROM students");
    if (Number(count) === 0) {
      const rows = await Promise.all(
        STUDENTS.map(async ([id, name]) => [id, name, await hashPassword(TEST_PASSWORD)]),
      );
      await conn.query("INSERT IGNORE INTO students (id, name, password_hash) VALUES ?", [rows]);
    }
  } finally {
    await conn.end();
  }
}

function openPool(credentials) {
  const old = pool;
  pool = mysql.createPool({
    ...connectionOptions(credentials),
    database: DB_NAME,
    connectionLimit: Number(env.DB_POOL_SIZE || 5),
    enableKeepAlive: true,
  });
  old?.end().catch(() => {});
}

export async function connectDatabase(load) {
  loadCredentials = load;
  const credentials = await load();
  await prepare(credentials);
  openPool(credentials);
}

// RDS가 관리하는 마스터 비밀번호는 주기적으로 바뀐다. 접속이 거부되면 비밀을 다시 읽어 풀을 새로 만든다.
export async function db(work) {
  try {
    return await work(pool);
  } catch (err) {
    if (err.code !== "ER_ACCESS_DENIED_ERROR") throw err;
    reopening ??= loadCredentials()
      .then(openPool)
      .finally(() => {
        reopening = null;
      });
    await reopening;
    return work(pool);
  }
}

export function transaction(work) {
  return db(async (p) => {
    const conn = await p.getConnection();
    try {
      await conn.beginTransaction();
      const result = await work(conn);
      await conn.commit();
      return result;
    } catch (err) {
      await conn.rollback().catch(() => {});
      throw err;
    } finally {
      conn.release();
    }
  });
}
