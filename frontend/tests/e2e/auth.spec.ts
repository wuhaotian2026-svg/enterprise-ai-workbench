import { expect, test } from "@playwright/test";

test("employee can log in and recover after session expiry", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill("employee");
  await page.getByLabel("密码").fill("task18-employee-password");
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByRole("heading", { name: /从制度原文开始/ })).toBeVisible();
  await page.context().clearCookies();
  await page.reload();
  await expect(page.getByRole("button", { name: /登录/ })).toBeVisible();
});

test("employee workspace remains usable at 500px", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 1200 });
  await page.goto("/");
  await page.getByLabel("用户名").fill("employee");
  await page.getByLabel("密码").fill("task18-employee-password");
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByLabel("向制度知识库提问")).toBeVisible();
  await page.screenshot({ path: "../output/playwright/task20-workspace-mobile.png", fullPage: true });
});
