import { expect, test, type Page } from "@playwright/test";

const alicePassword = process.env.E2E_ALICE_PASSWORD;
const managerPassword = process.env.E2E_MANAGER_PASSWORD;
const specialistPassword = process.env.E2E_PROCUREMENT_PASSWORD;
const visualOutputDir = process.env.E2E_VISUAL_OUTPUT_DIR;

type SubmittedRequest = {
  id: string;
  request_number: string;
  title: string;
  status: string;
};

async function capture(page: Page, name: string) {
  if (!visualOutputDir) return;
  await page.screenshot({ path: `${visualOutputDir}/${name}`, fullPage: true });
}

async function login(page: Page, username: string, password: string | undefined) {
  if (!password) throw new Error(`Missing E2E password for ${username}`);
  await page.goto("/");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByRole("button", { name: "退出" })).toBeVisible({ timeout: 15_000 });
}

async function logout(page: Page) {
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByRole("button", { name: /登录/ })).toBeVisible();
}

async function openModule(page: Page, name: "采购申请" | "审批中心") {
  await page.getByRole("button", { name }).click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
}

async function submitRequest(
  page: Page,
  title: string,
  captureVisuals = false,
): Promise<SubmittedRequest> {
  await openModule(page, "采购申请");
  await page.getByLabel("申请标题").fill(title);
  await page.getByLabel("期望到货日期").fill("2026-10-20");
  await page.getByLabel("采购用途").fill(`Task 17 浏览器验收：${title}`);
  await page.getByLabel("品类 1").selectOption("it_equipment");
  await page.getByLabel("物品名称 1").fill("人体工学键盘");
  await page.getByLabel("规格说明 1").fill("虚构演示规格，不对应真实采购");
  await page.getByLabel("数量 1").fill("2");
  await page.getByLabel("单位 1").fill("套");
  await page.getByLabel("预估单价 1").fill("399.50");
  await expect(page.getByText("服务器权威总额 ¥799.00")).toBeVisible();
  await expect(page.getByRole("button", { name: "检查并提交" })).toBeEnabled();
  if (captureVisuals) await capture(page, "01-procurement-form-desktop.png");
  await page.getByRole("button", { name: "检查并提交" }).click();
  const confirmation = page.getByRole("dialog", { name: "提交采购申请确认" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("直属部门负责人审批 → 采购专员复核 → 完成");
  if (captureVisuals) await capture(page, "02-submit-confirmation-desktop.png");
  const responsePromise = page.waitForResponse(
    response => response.url().endsWith("/api/v1/procurement/requests")
      && response.request().method() === "POST",
  );
  await confirmation.getByRole("button", { name: "确认提交" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const submitted = await response.json() as SubmittedRequest;
  await expect(
    page.locator(".procurement-record").filter({ hasText: submitted.request_number }),
  ).toBeVisible();
  return submitted;
}

async function decide(
  page: Page,
  title: string,
  decision: "approve" | "reject",
  reason = "",
  captureVisuals = false,
) {
  await openModule(page, "审批中心");
  const task = page.getByRole("button", { name: new RegExp(`${title}，待处理`) });
  await expect(task).toBeVisible();
  await task.click();
  const detail = page.getByRole("dialog", { name: /采购申请 PR-/ });
  await expect(detail).toContainText(title);
  if (captureVisuals) await capture(page, "04-approval-queue-detail-desktop.png");
  await detail.getByRole("button", { name: decision === "approve" ? "批准" : "拒绝" }).click();
  const confirmation = page.getByRole("dialog", {
    name: decision === "approve" ? "批准审批任务确认" : "拒绝审批任务确认",
  });
  await expect(confirmation).toBeVisible();
  if (decision === "reject") await confirmation.getByLabel("拒绝理由").fill(reason);
  if (captureVisuals && decision === "reject") {
    await capture(page, "05-rejection-dialog-desktop.png");
  }
  const responsePromise = page.waitForResponse(
    response => response.url().includes("/api/v1/approvals/tasks/")
      && response.url().endsWith(decision === "approve" ? "/approve" : "/reject"),
  );
  await confirmation.getByRole("button", {
    name: decision === "approve" ? "确认批准" : "确认拒绝",
  }).click();
  expect((await responsePromise).status()).toBe(200);
  await expect(page.getByRole("alert")).toContainText("审批决定已提交");
}

function runTitle(prefix: string): string {
  return `${prefix}-${Date.now().toString(36).toUpperCase()}`;
}

async function requestAiSubmission(page: Page, title: string) {
  const prompt = [
    `请提交采购申请，标题：${title}`,
    "用途：Task 17 AI Tool Calling 浏览器验收",
    "需要日期 2026-10-20",
    "币种 CNY",
    "明细：人体工学键盘，分类：it_equipment，规格：虚构演示规格，2套，单价399.50元。",
  ].join("；");
  await page.getByLabel("向采购 AI 助手说明事项").fill(prompt);
  await page.getByRole("button", { name: "发送给采购 AI 助手" }).click();
  const card = page.getByRole("group", { name: "采购 AI 提交确认" });
  await expect(card).toBeVisible({ timeout: 90_000 });
  await expect(card).toContainText(title);
  await expect(card).toContainText("¥799.00 CNY");
  return card;
}

test.describe.serial("procurement two-level approval journeys", () => {
  test("Alice submits, manager approves, specialist approves, and Alice sees completion", async ({ page }) => {
    test.setTimeout(180_000);
    const title = runTitle("Task17-两级通过");
    await login(page, "alice.hr.demo", alicePassword);
    const submitted = await submitRequest(page, title, true);
    expect(submitted.status).toBe("pending_manager");
    await logout(page);

    await login(page, "manager.procurement.demo", managerPassword);
    await decide(page, title, "approve");
    await logout(page);

    await login(page, "specialist.procurement.demo", specialistPassword);
    await decide(page, title, "approve");
    await logout(page);

    await login(page, "alice.hr.demo", alicePassword);
    await openModule(page, "采购申请");
    const record = page.locator(".procurement-record").filter({ hasText: submitted.request_number });
    await expect(record).toContainText("已完成");
    await record.getByRole("button", { name: `查看 ${submitted.request_number}` }).click();
    const detail = page.getByRole("dialog", { name: `采购申请 ${submitted.request_number}` });
    await expect(detail).toContainText("已完成");
    await expect(detail.getByRole("list", { name: "审批时间线" })).toContainText("采购专员复核");
    await capture(page, "03-request-detail-desktop.png");
  });

  test("Alice withdraws after manager approval and before specialist review", async ({ page }) => {
    test.setTimeout(180_000);
    const title = runTitle("Task17-中途撤回");
    await login(page, "alice.hr.demo", alicePassword);
    const submitted = await submitRequest(page, title);
    await logout(page);

    await login(page, "manager.procurement.demo", managerPassword);
    await decide(page, title, "approve");
    await logout(page);

    await login(page, "alice.hr.demo", alicePassword);
    await openModule(page, "采购申请");
    const record = page.locator(".procurement-record").filter({ hasText: submitted.request_number });
    await expect(record).toContainText("待采购专员复核");
    await record.getByRole("button", { name: `查看 ${submitted.request_number}` }).click();
    await page.getByRole("button", { name: `撤回 ${submitted.request_number}` }).click();
    const confirmation = page.getByRole("dialog", { name: "撤回采购申请确认" });
    await expect(confirmation).toBeVisible();
    const responsePromise = page.waitForResponse(
      response => response.url().endsWith(`/api/v1/procurement/requests/${submitted.id}/withdraw`),
    );
    await confirmation.getByRole("button", { name: "确认撤回" }).click();
    expect((await responsePromise).status()).toBe(200);
    await expect(page.getByRole("alert")).toContainText("已撤回");
    await expect(record).toContainText("已撤回");
  });

  test("a rejection terminates the request and copy creates a new request number", async ({ page }) => {
    test.setTimeout(180_000);
    const title = runTitle("Task17-拒绝复制");
    await login(page, "alice.hr.demo", alicePassword);
    const rejected = await submitRequest(page, title);
    await logout(page);

    await login(page, "manager.procurement.demo", managerPassword);
    await decide(
      page,
      title,
      "reject",
      "虚构演示：当前申请规格依据不足",
      true,
    );
    await logout(page);

    await login(page, "alice.hr.demo", alicePassword);
    await openModule(page, "采购申请");
    const rejectedRecord = page.locator(".procurement-record").filter({ hasText: rejected.request_number });
    await expect(rejectedRecord).toContainText("已拒绝");
    await rejectedRecord.getByRole("button", { name: `查看 ${rejected.request_number}` }).click();
    await page.getByRole("button", { name: `复制 ${rejected.request_number} 新建` }).click();
    await expect(page.getByRole("alert")).toContainText("将生成新的申请编号和审批实例");
    await expect(page.getByLabel("申请标题")).toHaveValue(title);
    const copiedTitle = `${title}-复制`;
    await page.getByLabel("申请标题").fill(copiedTitle);
    await expect(page.getByRole("button", { name: "检查并提交" })).toBeEnabled();
    await page.getByRole("button", { name: "检查并提交" }).click();
    const responsePromise = page.waitForResponse(
      response => response.url().endsWith("/api/v1/procurement/requests")
        && response.request().method() === "POST",
    );
    await page.getByRole("dialog", { name: "提交采购申请确认" })
      .getByRole("button", { name: "确认提交" }).click();
    const response = await responsePromise;
    expect(response.status()).toBe(201);
    const copied = await response.json() as SubmittedRequest;
    expect(copied.request_number).not.toBe(rejected.request_number);
    expect(copied.id).not.toBe(rejected.id);
    await expect(
      page.locator(".procurement-record").filter({ hasText: copied.request_number }),
    ).toBeVisible();
  });

  test("Alice cancels and then confirms real-model procurement Tool Calling proposals", async ({ page }) => {
    test.setTimeout(300_000);
    await login(page, "alice.hr.demo", alicePassword);
    await openModule(page, "采购申请");

    const cancelledTitle = runTitle("Task17-AI取消");
    let card = await requestAiSubmission(page, cancelledTitle);
    await card.scrollIntoViewIfNeeded();
    await expect(card).toBeInViewport();
    await capture(page, "08-ai-confirmation-desktop.png");
    await page.setViewportSize({ width: 500, height: 900 });
    await card.scrollIntoViewIfNeeded();
    await expect(card).toBeInViewport();
    await capture(page, "09-ai-confirmation-mobile.png");
    await page.setViewportSize({ width: 1280, height: 720 });

    const cancelResponse = page.waitForResponse(response => (
      response.url().includes("/api/v1/procurement/confirmations/")
      && response.url().endsWith("/cancel")
      && response.request().method() === "POST"
    ));
    await card.getByRole("button", { name: "返回修改 AI 采购申请" }).click();
    expect((await cancelResponse).status()).toBe(200);
    await expect(card.getByRole("status")).toContainText("已取消这次写操作提案");
    await expect(page.locator(".procurement-record").filter({ hasText: cancelledTitle })).toHaveCount(0);

    const confirmedTitle = runTitle("Task17-AI确认");
    card = await requestAiSubmission(page, confirmedTitle);
    const confirmResponse = page.waitForResponse(response => (
      response.url().includes("/api/v1/procurement/confirmations/")
      && response.url().endsWith("/confirm")
      && response.request().method() === "POST"
    ));
    await card.getByRole("button", { name: "确认提交 AI 采购申请" }).click();
    expect((await confirmResponse).status()).toBe(200);
    await expect(card.getByRole("status")).toContainText("采购申请已创建，审批流程已启动");
    await expect(page.locator(".procurement-record").filter({ hasText: confirmedTitle }))
      .toBeVisible({ timeout: 30_000 });
  });
});
