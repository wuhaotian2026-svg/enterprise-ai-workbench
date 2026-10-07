import { chromium } from "../frontend/node_modules/@playwright/test/index.mjs";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

const baseURL = process.env.TASK31_BASE_URL ?? "http://localhost:8081";
const adminPassword = process.env.TASK31_ADMIN_PASSWORD;
const employeePassword = process.env.TASK31_EMPLOYEE_PASSWORD;
if (!adminPassword || !employeePassword) throw new Error("Task 31 demo passwords are required");

const output = new URL("../test-results/task31/", import.meta.url);
await fs.mkdir(output, { recursive: true });
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});
const results = { documents: [], previews: {}, questions: [], consoleErrors: [] };
let docxPreviewHref = "";

async function login(page, username, password) {
  await page.goto(baseURL);
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
}

const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const adminPage = await admin.newPage();
adminPage.on("console", message => { if (message.type() === "error") results.consoleErrors.push(message.text()); });
await login(adminPage, "admin", adminPassword);
await adminPage.getByRole("heading", { name: "制度资料管理" }).waitFor();
await adminPage.waitForFunction(() => document.querySelectorAll('.document-row:not(.header) > strong').length === 8);
results.documents = await adminPage.locator('.document-row:not(.header) > strong').allTextContents();
if (results.documents.length !== 8) throw new Error(`Expected 8 documents, found ${results.documents.length}`);
const screenshotPath = name => fileURLToPath(new URL(name, output));
await adminPage.screenshot({ path: screenshotPath("admin-register-desktop.png"), fullPage: true });

for (const name of ["真实DOCX格式验收.docx", "真实DOCX格式验收.pdf"]) {
  await adminPage.getByRole("button", { name: `查看 ${name}` }).click();
  const dialog = adminPage.getByRole("dialog", { name });
  await dialog.waitFor();
  const preview = dialog.getByRole("link", { name: "在线预览" });
  const href = await preview.getAttribute("href");
  if (name.endsWith(".docx")) docxPreviewHref = href;
  const response = await admin.request.get(`${baseURL}${href}`);
  results.previews[name] = { status: response.status(), contentType: response.headers()["content-type"], bytes: (await response.body()).length };
  if (response.status() !== 200 || !results.previews[name].contentType?.startsWith("application/pdf")) throw new Error(`Preview failed for ${name}`);
  if (name.endsWith(".docx")) {
    if (!(await dialog.getByText("4 个当前活动片段").isVisible())) throw new Error("DOCX chunk count mismatch");
    const downloadHref = await dialog.getByRole("link", { name: "下载原文件" }).getAttribute("href");
    const download = await admin.request.get(`${baseURL}${downloadHref}`);
    if (download.status() !== 200 || !download.headers()["content-type"]?.includes("wordprocessingml")) throw new Error("DOCX download contract failed");
    await adminPage.screenshot({ path: screenshotPath("docx-detail-desktop.png") });
    await adminPage.setViewportSize({ width: 500, height: 900 });
    await adminPage.screenshot({ path: screenshotPath("docx-detail-mobile.png") });
    await adminPage.setViewportSize({ width: 1440, height: 1000 });
  }
  await dialog.getByRole("button", { name: "关闭文档详情" }).click();
}

await adminPage.getByRole("button", { name: "查看 员工休假与考勤制度.txt" }).click();
const txtDialog = adminPage.getByRole("dialog", { name: "员工休假与考勤制度.txt" });
await txtDialog.getByText("此格式请查看系统解析内容").waitFor();
if (await txtDialog.getByRole("link", { name: "在线预览" }).count()) throw new Error("TXT unexpectedly offers preview");
await adminPage.screenshot({ path: screenshotPath("txt-detail-desktop.png") });

await admin.close();

const employee = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const employeePage = await employee.newPage();
employeePage.on("console", message => { if (message.type() === "error") results.consoleErrors.push(message.text()); });
await login(employeePage, "employee", employeePassword);
await employeePage.getByRole("heading", { name: /从制度原文开始/ }).waitFor();
const employeeForbidden = await employee.request.get(`${baseURL}${docxPreviewHref}`);
results.previews.employeeRoleProbe = { status: employeeForbidden.status() };
if (employeeForbidden.status() !== 403) throw new Error(`Expected employee preview 403, got ${employeeForbidden.status()}`);
const questions = process.env.TASK31_SKIP_QA === "1" ? [] : ["我想请假一周，有什么需要注意的吗？", "出差住宿标准是多少？", "费用报销需要哪些材料？", "采购申请需要谁审批？"];
for (const question of questions) {
  await employeePage.getByLabel("向制度知识库提问").fill(question);
  await employeePage.getByRole("button", { name: "发送问题" }).click();
  await employeePage.locator(".answer-document h1", { hasText: question }).waitFor({ timeout: 60000 });
  const answer = await employeePage.locator(".answer-document").innerText();
  results.questions.push({ question, answer: answer.slice(0, 2000) });
}
if (!questions.length) {
  await employeePage.locator(".history-item").first().click();
  try { results.questions = JSON.parse(await fs.readFile(new URL("results.json", output), "utf8")).questions ?? []; } catch {}
}
await employeePage.screenshot({ path: screenshotPath("employee-answer-desktop.png"), fullPage: true });
await employeePage.setViewportSize({ width: 500, height: 900 });
await employeePage.getByRole("button", { name: "打开提问历史" }).click();
await employeePage.locator(".history-item").first().click();
await employeePage.locator(".history-drawer:not(.open)").waitFor();
await employeePage.waitForTimeout(300);
await employeePage.screenshot({ path: screenshotPath("employee-answer-mobile.png") });
await employee.close();
await browser.close();
await fs.writeFile(new URL("results.json", output), JSON.stringify(results, null, 2), "utf8");
console.log(JSON.stringify(results, null, 2));
