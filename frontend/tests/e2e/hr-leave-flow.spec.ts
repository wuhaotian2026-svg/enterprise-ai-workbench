import { expect, test, type Page } from "@playwright/test";

const alicePassword = process.env.E2E_ALICE_PASSWORD;
const helenPassword = process.env.E2E_HELEN_PASSWORD;
const adminPassword = process.env.E2E_ADMIN_PASSWORD;

type FunnelResponse = {
  metrics: Record<string, { value: number | null }>;
};

async function login(page: Page, username: string, password: string | undefined) {
  if (!password) throw new Error(`Missing E2E password for ${username}`);
  await page.goto("/");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByRole("button", { name: "退出" })).toBeVisible();
}

async function logout(page: Page) {
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByRole("button", { name: /登录/ })).toBeVisible();
}

async function loadHrFunnel(page: Page): Promise<FunnelResponse> {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/analytics/hr-funnel");
    if (!response.ok) throw new Error(`HR funnel request failed: ${response.status}`);
    return response.json();
  });
}

test("employee balance to confirmed request to human HR approval", async ({ page }) => {
  await login(page, "admin.hr.demo", adminPassword);
  const beforeFunnel = await loadHrFunnel(page);
  await logout(page);
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "HR 办事助手" }).click();

  await page.locator("#hr-assistant-input").fill("查询我的 2026 年年假余额");
  const balanceResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hr/conversations/") && response.url().endsWith("/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  expect((await balanceResponse).ok()).toBe(true);
  const balanceTurn = page.locator(".hr-turn").filter({ hasText: "查询我的 2026 年年假余额" }).last();
  await expect(balanceTurn.getByRole("heading", { name: "年假余额" }).first()).toBeVisible();

  await page.locator("#hr-assistant-input").fill("申请年假：2026-09-07 到 2026-09-09，Task16 浏览器闭环验证");
  const proposalResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hr/conversations/") && response.url().endsWith("/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  const proposal = await (await proposalResponse).json() as {
    blocks: Array<{ type: string; confirmation_id?: string }>;
  };
  const confirmationId = proposal.blocks.find((block) => block.type === "confirmation")?.confirmation_id;
  expect(confirmationId).toBeTruthy();
  const proposalTurn = page.locator(".hr-turn")
    .filter({ hasText: "Task16 浏览器闭环验证" })
    .last();
  await expect(proposalTurn.getByRole("heading", { name: "提交请假申请" }).last()).toBeVisible();
  const confirmRequestPromise = page.waitForRequest(
    (request) => request.url().endsWith(`/hr/confirmations/${confirmationId}/confirm`),
  );
  await proposalTurn.getByRole("button", { name: "确认提交" }).last().click();
  const confirmRequest = await confirmRequestPromise;
  await expect(proposalTurn.getByRole("heading", { name: "已提交，等待 HR 审核" }).last()).toBeVisible();

  const replayStatus = await page.evaluate(async ({ id, body, csrf }) => {
    const response = await fetch(`/api/v1/hr/confirmations/${id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body,
    });
    return response.status;
  }, {
    id: confirmationId,
    body: confirmRequest.postData() ?? "{}",
    csrf: confirmRequest.headers()["x-csrf-token"] ?? "",
  });
  expect(replayStatus).toBe(200);

  await page.getByRole("button", { name: "我的申请" }).click();
  const created = page.locator(".hr-request-list article").filter({ hasText: "Task16 浏览器闭环验证" });
  await expect(created).toHaveCount(1);
  const requestNumber = (await created.locator("header span").first().textContent())?.trim();
  expect(requestNumber).toMatch(/^LR-/);

  await logout(page);
  await login(page, "admin.hr.demo", adminPassword);
  const afterFunnel = await loadHrFunnel(page);
  for (const [metric, minimumIncrease] of [
    ["hr_turns_submitted", 2],
    ["intents_resolved", 2],
    ["write_tools_planned", 1],
    ["confirmations_shown", 1],
    ["confirmations_confirmed", 1],
    ["leave_requests_submitted", 1],
  ] as const) {
    expect(afterFunnel.metrics[metric].value).toBeGreaterThanOrEqual(
      (beforeFunnel.metrics[metric].value ?? 0) + minimumIncrease,
    );
  }
  await page.getByRole("button", { name: "运营驾驶舱" }).click();
  const submittedStage = page.getByRole("listitem").filter({ hasText: "请假提交" });
  await expect(submittedStage).toContainText(
    String(afterFunnel.metrics.leave_requests_submitted.value),
  );
  await logout(page);
  await login(page, "helen.hr.demo", helenPassword);
  await page.getByRole("button", { name: "HR 审核" }).click();
  await page.getByRole("button", { name: `审核 ${requestNumber}` }).click();
  await expect(page.getByLabel("申请审核详情")).toContainText("Task16 浏览器闭环验证");
  await page.getByRole("button", { name: "批准申请" }).click();
  await expect(page.getByRole("alert")).toContainText("申请已批准");

  await logout(page);
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "我的申请" }).click();
  await expect(
    page.locator(".hr-request-list article").filter({ hasText: requestNumber ?? "missing-request" }),
  ).toContainText("已批准");
});

test("employee cancels only a pending request after explicit confirmation", async ({ page }) => {
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "我的申请" }).click();
  const pending = page.locator(".hr-request-list article").filter({ hasText: "虚构演示：待审批申请" });
  await expect(pending).toContainText("待审批");
  await pending.getByRole("button", { name: /撤销/ }).click();
  await expect(page.getByRole("dialog", { name: "撤销申请确认" })).toBeVisible();
  await page.getByRole("button", { name: "确认撤销" }).click();
  await expect(page.getByRole("alert")).toContainText("申请已撤销");
  const cancelled = page.locator(".hr-request-list article").filter({ hasText: "虚构演示：待审批申请" });
  await expect(cancelled).toContainText("已撤销");
  await expect(cancelled.getByRole("button", { name: /撤销/ })).toHaveCount(0);
});
