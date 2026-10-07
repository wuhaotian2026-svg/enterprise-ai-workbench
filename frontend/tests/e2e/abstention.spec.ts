import { expect, test } from "@playwright/test";

test("unsupported policy question renders strict abstention", async ({ page }) => {
  const question = { id: "44444444-4444-4444-4444-444444444444", text: "公司是否提供宠物保险？", status: "completed", created_at: "2026-08-13T00:00:00Z", answer: { id: "55555555-5555-5555-5555-555555555555", status: "abstained", text: null, refusal_reason: "no_evidence", evidence_score: 0.2, citations: [] } };
  await page.route("**/api/v1/questions", async route => route.request().method() === "POST" ? route.fulfill({ json: question }) : route.fulfill({ json: [] }));
  await page.goto("/");
  await page.getByLabel("用户名").fill("employee");
  await page.getByLabel("密码").fill("task18-employee-password");
  await page.getByRole("button", { name: /登录/ }).click();
  await page.getByLabel("向制度知识库提问").fill(question.text);
  await page.getByRole("button", { name: "发送问题" }).click();
  await expect(page.getByRole("heading", { name: "无法从现有制度确认" })).toBeVisible();
  await expect(page.getByText(/请咨询人力资源或行政负责人/)).toBeVisible();
  await page.screenshot({ path: "../output/playwright/task20-abstention-desktop.png", fullPage: true });
});
