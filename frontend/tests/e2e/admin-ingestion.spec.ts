import { expect, test } from "@playwright/test";

const adminPassword = process.env.E2E_ADMIN_PASSWORD;

async function enterDocumentAdministration(page: import("@playwright/test").Page) {
  if (!adminPassword) throw new Error("Missing E2E password for admin.hr.demo");
  await page.goto("/");
  await page.getByLabel("用户名").fill("admin.hr.demo");
  await page.getByLabel("密码").fill(adminPassword);
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByRole("button", { name: "退出" })).toBeVisible();
  const knowledgeManagement = page.getByRole("button", { name: "知识管理" });
  await expect(knowledgeManagement).toBeAttached();
  const menu = page.locator(".workbench-menu");
  if (await menu.isVisible()) {
    await menu.click();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
    await page.waitForTimeout(250);
  }
  await knowledgeManagement.scrollIntoViewIfNeeded();
  await knowledgeManagement.click();
  await expect(page.getByRole("heading", { name: "制度资料管理" })).toBeVisible();
}

test("employee cannot enter document administration", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill("employee");
  await page.getByLabel("密码").fill("task18-employee-password");
  await page.getByRole("button", { name: /登录/ }).click();
  await expect(page.getByRole("heading", { name: "制度资料管理" })).toHaveCount(0);
});

test("administrator sees the upload registry", async ({ page }) => {
  await enterDocumentAdministration(page);
  await expect(page.locator('input[type="file"]')).toBeAttached();
  await expect(page.getByText("更新 / 片段")).toBeVisible();
  await expect(page.getByText("差旅费用管理制度.txt")).toBeVisible();
  await page.screenshot({ path: "../output/playwright/task20-admin-desktop.png", fullPage: true });
});

test("administrator registry remains usable at 500px", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 900 });
  await enterDocumentAdministration(page);
  await expect(page.getByText("差旅费用管理制度.txt")).toBeVisible();
  await expect(page.getByRole("button", { name: /重新索引|停用/ }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.screenshot({ path: "../output/playwright/task22-admin-mobile.png", fullPage: true });
});
