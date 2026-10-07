import { expect, test } from "@playwright/test";

test("supported answer exposes expandable evidence", async ({ page }) => {
  const question = { id: "11111111-1111-1111-1111-111111111111", text: "一线城市住宿标准是多少？", status: "completed", created_at: "2026-08-13T00:00:00Z", answer: { id: "22222222-2222-2222-2222-222222222222", status: "answered", text: "一线城市住宿标准为每晚 500 元。", refusal_reason: null, evidence_score: 0.91, citations: [{ number: 1, chunk_id: "33333333-3333-3333-3333-333333333333", evidence_snapshot: "一线城市住宿标准每人每晚不超过 500 元。", source_status: "enabled" }] } };
  const clarification = { id: "44444444-4444-4444-8444-444444444444", text: "南京出差三天多少钱？", status: "completed", created_at: "2026-08-18T00:00:00Z", answer: { id: "55555555-5555-4555-8555-555555555555", status: "needs_clarification", text: "其他城市住宿标准为350元/晚。", refusal_reason: null, evidence_score: 0.87, citations: [{ number: 1, chunk_id: "66666666-6666-4666-8666-666666666666", evidence_snapshot: "其他城市住宿费每人每晚不超过350元。", source_status: "enabled", document_name: "差旅费用管理制度.txt", mime_type: "text/plain", page_number: null, heading_path: "住宿标准", location: "paragraph:8" }], clarification: { questions: ["适用哪一城市档位？", "实际住宿几晚？"] } } };
  let loggedIn = false;
  await page.route("**/api/v1/auth/me", async route => loggedIn
    ? route.fulfill({ json: { username: "employee", role: "employee" } })
    : route.fulfill({ status: 401, json: { code: "auth_required" } }));
  await page.route("**/api/v1/auth/login", async route => {
    loggedIn = true;
    await route.fulfill({ status: 204 });
  });
  await page.route("**/api/v1/workbench/modules", async route => route.fulfill({ json: {
    catalog_version: "2026-08-16",
    modules: [{ key: "knowledge", label: "制度知识库", index: "01" }],
  } }));
  await page.route("**/api/v1/analytics/ui-events", async route => route.fulfill({ status: 204 }));
  await page.route("**/api/v1/questions", async route => {
    if (route.request().method() !== "POST") return route.fulfill({ json: [] });
    const body = route.request().postDataJSON() as { text?: string };
    return route.fulfill({ json: body.text === clarification.text ? clarification : question });
  });
  await page.goto("/");
  await page.getByLabel("用户名").fill("employee");
  await page.getByLabel("密码").fill("task18-employee-password");
  await page.getByRole("button", { name: /登录/ }).click();
  await page.getByLabel("向制度知识库提问").fill(question.text);
  await page.getByRole("button", { name: "发送问题" }).click();
  await expect(page.getByText(question.answer.text)).toBeVisible();
  await page.getByRole("button", { name: /查看引用 1/ }).click();
  await expect(page.getByText(question.answer.citations[0].evidence_snapshot)).toBeVisible();

  await page.getByLabel("向制度知识库提问").fill(clarification.text);
  await page.getByRole("button", { name: "发送问题" }).click();
  await expect(page.getByText("NEEDS CLARIFICATION / 需要补充信息")).toBeVisible();
  await expect(page.getByText(clarification.answer.text)).toBeVisible();
  await expect(page.getByText("适用哪一城市档位？")).toBeVisible();
  await expect(page.getByText("实际住宿几晚？")).toBeVisible();
  await expect(page.getByText("STRICT ABSTENTION")).toHaveCount(0);
  await page.getByLabel("向制度知识库提问").fill("南京属于其他城市，住两晚");
  await page.getByText("适用哪一城市档位？").click();
  await expect(page.getByLabel("向制度知识库提问")).toHaveValue("南京属于其他城市，住两晚");
  await page.screenshot({ path: "../output/playwright/task11-rag-clarification-desktop.png", fullPage: true });
});
