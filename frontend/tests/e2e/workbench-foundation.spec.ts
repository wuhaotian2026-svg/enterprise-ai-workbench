import { expect, test, type Page, type Request } from "@playwright/test";

const alicePassword = process.env.E2E_ALICE_PASSWORD;
const helenPassword = process.env.E2E_HELEN_PASSWORD;
const adminPassword = process.env.E2E_ADMIN_PASSWORD;
const managerPassword = process.env.E2E_MANAGER_PASSWORD;
const procurementPassword = process.env.E2E_PROCUREMENT_PASSWORD;
const visualOutputDir = process.env.E2E_VISUAL_OUTPUT_DIR;

type ApiResult<T> = { status: number; body: T };
type OrganizationUnit = {
  id: string;
  code: string;
  name: string;
  parent_id: string | null;
  is_active: boolean;
};
type Employee = {
  id: string;
  user_id: string;
  display_name: string;
  organization_unit_id: string | null;
};
type Grant = {
  id: string;
  user_id: string;
  capability: string;
  scope_kind: string;
  organization_unit_id: string | null;
  is_active: boolean;
};
type ReviewItem = { id: string; request_number: string };
type LeaveRequest = ReviewItem & {
  reason: string;
  status: string;
  start_date: string;
  end_date: string;
};

function selectAvailableLeaveDate(requests: LeaveRequest[]) {
  const blocking = requests.filter((item) => ["pending", "approved"].includes(item.status));
  for (
    let candidate = new Date("2026-10-12T00:00:00Z");
    candidate <= new Date("2026-10-30T00:00:00Z");
    candidate.setUTCDate(candidate.getUTCDate() + 1)
  ) {
    const weekday = candidate.getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    const isoDate = candidate.toISOString().slice(0, 10);
    if (!blocking.some((item) => item.start_date <= isoDate && isoDate <= item.end_date)) {
      return isoDate;
    }
  }
  throw new Error("No available E2E leave date");
}

async function login(
  page: Page,
  username: string,
  password: string | undefined,
) {
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

async function apiGet<T>(page: Page, path: string): Promise<ApiResult<T>> {
  return page.evaluate(async (requestPath) => {
    const response = await fetch(`/api/v1${requestPath}`);
    return { status: response.status, body: await response.json() };
  }, path);
}

async function replay<T>(page: Page, request: Request): Promise<ApiResult<T>> {
  return page.evaluate(async ({ path, body, csrf }) => {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
      },
      body,
    });
    return { status: response.status, body: await response.json() };
  }, {
    path: new URL(request.url()).pathname,
    body: request.postData() ?? "{}",
    csrf: request.headers()["x-csrf-token"] ?? "",
  });
}

async function expectOriginalResponse(request: Request, expectedStatus: number) {
  const response = await request.response();
  expect(response).not.toBeNull();
  expect(response?.status()).toBe(expectedStatus);
}

async function assignAlice(
  page: Page,
  organizationLabel: string,
  managerLabel?: string,
) {
  await page.getByRole("button", { name: "编辑 Alice（虚构演示员工）" }).click();
  await page.getByLabel("主组织").selectOption({ label: organizationLabel });
  if (managerLabel) {
    await page.getByLabel("直属经理").selectOption({ label: managerLabel });
  }
  const assignmentRequest = page.waitForRequest(
    (request) => request.url().includes("/organization/employees/")
      && request.url().endsWith("/assignment"),
  );
  await page.getByRole("button", { name: "保存员工归属" }).click();
  await expect(page.getByRole("heading", { name: "Alice（虚构演示员工）" })).toHaveCount(0);
  return assignmentRequest;
}

test("admin organization controls enforce idempotency, scoped HR, and empty analytics", async ({ page }) => {
  test.setTimeout(180_000);
  const runSuffix = Date.now().toString(36).toUpperCase();
  const organizationCode = `TASK16-E2E-${runSuffix}`;
  const organizationName = `Task16 演示交付组 ${runSuffix}`;
  const requestMarker = `Task16 范围权限验证 ${runSuffix}`;
  await login(page, "admin.hr.demo", adminPassword);

  for (const moduleName of ["知识管理", "企业组织", "运营驾驶舱"]) {
    await expect(page.getByRole("button", { name: moduleName })).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "HR 办事助手" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "HR 审核" })).toHaveCount(0);

  await page.getByRole("button", { name: "企业组织" }).click();
  await expect(page.getByRole("heading", { name: "企业组织" })).toBeVisible();
  await page.getByRole("button", { name: "新增部门" }).click();
  await page.getByLabel("部门编码").fill(organizationCode);
  await page.getByLabel("部门名称").fill(organizationName);
  await page.getByLabel("上级组织").selectOption({ label: "产品中心" });
  const createRequestPromise = page.waitForRequest(
    (request) => request.url().endsWith("/organization/units")
      && request.method() === "POST",
  );
  await page.getByRole("button", { name: "保存部门" }).click();
  const createRequest = await createRequestPromise;
  await expect(page.getByRole("button", { name: new RegExp(organizationName) })).toBeVisible();
  await expectOriginalResponse(createRequest, 201);
  const createReplay = await replay<OrganizationUnit>(page, createRequest);
  expect(createReplay.status).toBe(201);
  const units = await apiGet<OrganizationUnit[]>(page, "/organization/units");
  const createdUnits = units.body.filter((item) => item.code === organizationCode);
  expect(createdUnits).toHaveLength(1);
  expect(createReplay.body.id).toBe(createdUnits[0].id);

  const assignmentRequest = await assignAlice(page, organizationName);
  await expectOriginalResponse(assignmentRequest, 200);
  const assignmentReplay = await replay<Employee>(page, assignmentRequest);
  expect(assignmentReplay.status).toBe(200);
  const employees = await apiGet<Employee[]>(page, "/organization/employees");
  const alice = employees.body.find((item) => item.display_name.includes("Alice"));
  expect(alice?.organization_unit_id).toBe(createdUnits[0].id);

  const grantForm = page.locator("form.grant-create");
  await grantForm.locator("label").filter({ hasText: "授权员工" }).locator("select")
    .selectOption({ label: "Alice（虚构演示员工）" });
  await grantForm.locator("label").filter({ hasText: "能力" }).locator("select")
    .selectOption({ label: "请假审核" });
  await grantForm.locator("label").filter({ hasText: "范围" }).locator("select")
    .selectOption({ label: "组织及下级" });
  await grantForm.locator("label").filter({ hasText: "授权组织" }).locator("select")
    .selectOption({ label: organizationName });
  const grantRequestPromise = page.waitForRequest(
    (request) => request.url().endsWith("/organization/capability-grants")
      && request.method() === "POST",
  );
  await page.getByRole("button", { name: "添加授权" }).click();
  const grantRequest = await grantRequestPromise;
  await expectOriginalResponse(grantRequest, 201);
  const grantReplay = await replay<Grant>(page, grantRequest);
  expect(grantReplay.status).toBe(201);
  const grantsAfterCreate = await apiGet<Grant[]>(page, "/organization/capability-grants");
  const aliceReviewGrants = grantsAfterCreate.body.filter(
    (item) => item.user_id === alice?.user_id
      && item.capability === "hr.leave.review"
      && item.organization_unit_id === createdUnits[0].id,
  );
  expect(aliceReviewGrants).toHaveLength(1);
  expect(aliceReviewGrants[0].id).toBe(grantReplay.body.id);

  const grantTable = page.getByRole("table", { name: "能力授权列表" });
  const aliceGrantRow = grantTable.getByRole("row")
    .filter({ hasText: "Alice（虚构演示员工）" })
    .filter({ hasText: "hr.leave.review" })
    .filter({ hasText: organizationName });
  await aliceGrantRow.getByRole("button", { name: "撤销 hr.leave.review 授权" }).click();
  await page.getByLabel("我已确认撤销影响").check();
  const revokeRequestPromise = page.waitForRequest(
    (request) => request.url().includes(`/organization/capability-grants/${aliceReviewGrants[0].id}/revoke`),
  );
  await page.getByRole("button", { name: "确认撤销授权" }).click();
  const revokeRequest = await revokeRequestPromise;
  await expectOriginalResponse(revokeRequest, 200);
  const revokeReplay = await replay<Grant>(page, revokeRequest);
  expect(revokeReplay.status).toBe(200);
  expect(revokeReplay.body.id).toBe(aliceReviewGrants[0].id);
  expect(revokeReplay.body.is_active).toBe(false);
  const grantsAfterRevoke = await apiGet<Grant[]>(page, "/organization/capability-grants");
  expect(
    grantsAfterRevoke.body.filter(
      (item) => item.user_id === alice?.user_id
        && item.capability === "hr.leave.review"
        && item.organization_unit_id === createdUnits[0].id,
    ),
  ).toHaveLength(1);

  await logout(page);
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "HR 办事助手" }).click();
  await page.getByRole("button", { name: "新建办事" }).click();
  await expect(page.getByText("还没有办事记录")).toBeVisible();
  const assistantInput = page.locator("#hr-assistant-input");
  await expect(assistantInput).toBeEnabled();
  const requestsBeforeSubmission = await apiGet<LeaveRequest[]>(page, "/hr/leave-requests");
  const requestIdsBeforeSubmission = new Set(
    requestsBeforeSubmission.body.map((item) => item.id),
  );
  const leaveDate = selectAvailableLeaveDate(requestsBeforeSubmission.body);
  const requestText = `申请年假：${leaveDate} 到 ${leaveDate}，用于家庭事务（${requestMarker}）`;
  await assistantInput.fill(
    requestText,
  );
  await expect.poll(async () => {
    if (await assistantInput.inputValue() !== requestText) {
      await assistantInput.fill(requestText);
    }
    return assistantInput.inputValue();
  }).toBe(requestText);
  await expect(page.getByRole("button", { name: "发送" })).toBeEnabled();
  const proposalResponse = page.waitForResponse(
    (response) => response.url().includes("/api/v1/hr/conversations/") && response.url().endsWith("/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  expect((await proposalResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: "提交请假申请" })).toBeVisible();
  await page.getByRole("button", { name: "确认提交" }).click();
  await expect(page.getByRole("heading", { name: "已提交，等待 HR 审核" })).toBeVisible();
  const aliceRequests = await apiGet<LeaveRequest[]>(page, "/hr/leave-requests");
  const scopedRequest = aliceRequests.body.find(
    (item) => !requestIdsBeforeSubmission.has(item.id) && item.status === "pending",
  );
  expect(scopedRequest).toBeTruthy();
  await logout(page);
  await login(page, "helen.hr.demo", helenPassword);
  await page.getByRole("button", { name: "HR 审核" }).click();
  await expect(
    page.getByRole("button", {
      name: `审核 ${scopedRequest?.request_number ?? "missing"}`,
    }),
  ).toBeVisible();
  const visibleQueue = await apiGet<ReviewItem[]>(page, "/hr/review-queue?status=pending");
  const productRequest = visibleQueue.body.find(
    (item) => item.id === scopedRequest?.id,
  );
  expect(productRequest).toBeTruthy();

  await logout(page);
  await login(page, "admin.hr.demo", adminPassword);
  await page.getByRole("button", { name: "企业组织" }).click();
  await assignAlice(page, "人力中心", "未设置");
  await logout(page);
  await login(page, "helen.hr.demo", helenPassword);
  const hiddenDetail = await apiGet<Record<string, unknown>>(
    page,
    `/hr/review-queue/${productRequest?.id ?? "missing"}`,
  );
  expect(hiddenDetail.status).toBe(404);
  expect(hiddenDetail.body).toMatchObject({ code: "leave_request_not_found" });
  const hiddenQueue = await apiGet<ReviewItem[]>(page, "/hr/review-queue?status=pending");
  expect(hiddenQueue.body.some((item) => item.id === productRequest?.id)).toBe(false);

  await logout(page);
  await login(page, "admin.hr.demo", adminPassword);
  await page.getByRole("button", { name: "企业组织" }).click();
  await assignAlice(page, "产品中心", "Ming（虚构直属部门负责人）");
  await page.getByRole("button", { name: "运营驾驶舱" }).click();
  await expect(page.getByRole("heading", { name: "运营驾驶舱" })).toBeVisible();
  await page.getByLabel("组织范围").selectOption({ label: `${organizationName} · ${organizationCode}` });
  await expect(page.getByText("暂无数据").first()).toBeVisible();
  expect(await page.getByText("暂无数据").count()).toBeGreaterThan(0);
  await expect(page.getByText("100%", { exact: true })).toHaveCount(0);

  await logout(page);
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "我的申请" }).click();
  const createdRequest = page.locator(".hr-request-list article")
    .filter({ hasText: scopedRequest?.request_number ?? "missing-request" });
  await expect(createdRequest).toContainText("待审批");
  await createdRequest.getByRole("button", { name: /撤销/ }).click();
  await expect(page.getByRole("dialog", { name: "撤销申请确认" })).toBeVisible();
  await page.getByRole("button", { name: "确认撤销" }).click();
  await expect(page.getByRole("alert")).toContainText("申请已撤销");
});

test("mobile workbench navigation casts a shadow only while open", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 900 });
  await login(page, "admin.hr.demo", adminPassword);

  const railShadow = await page.locator(".workbench-rail").evaluate(
    (element) => getComputedStyle(element).boxShadow,
  );

  expect(railShadow).toBe("none");
  await page.getByRole("button", { name: "打开工作台导航" }).click();
  const openRailShadow = await page.locator(".workbench-rail").evaluate(
    (element) => getComputedStyle(element).boxShadow,
  );
  expect(openRailShadow).not.toBe("none");
});

test("mobile HR history casts a shadow only while open", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 900 });
  await login(page, "alice.hr.demo", alicePassword);
  await page.getByRole("button", { name: "打开工作台导航" }).click();
  await page.getByRole("button", { name: "HR 办事助手" }).click();
  await expect(page.getByRole("button", { name: "打开 HR 对话历史" })).toBeVisible();

  const historyShadow = await page.locator(".hr-history-drawer").evaluate(
    (element) => getComputedStyle(element).boxShadow,
  );

  expect(historyShadow).toBe("none");
  await page.getByRole("button", { name: "打开 HR 对话历史" }).click();
  const openHistoryShadow = await page.locator(".hr-history-drawer").evaluate(
    (element) => getComputedStyle(element).boxShadow,
  );
  expect(openHistoryShadow).not.toBe("none");
});

test("procurement reviewers receive the approval center without admin modules", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 500, height: 900 });
  for (const [username, password] of [
    ["manager.procurement.demo", managerPassword],
    ["specialist.procurement.demo", procurementPassword],
  ] as const) {
    await login(page, username, password);
    await page.getByRole("button", { name: "打开工作台导航" }).click();
    await expect(page.getByRole("button", { name: "审批中心" })).toBeVisible();
    await expect(page.getByRole("button", { name: "企业组织" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "运营驾驶舱" })).toHaveCount(0);
    if (username === "manager.procurement.demo" && visualOutputDir) {
      await page.getByRole("button", { name: "采购申请" }).click();
      await expect(page.getByRole("heading", { name: "采购申请", exact: true })).toBeVisible();
      await page.screenshot({
        path: `${visualOutputDir}/06-procurement-form-mobile.png`,
        fullPage: true,
      });
      await page.getByRole("button", { name: "打开工作台导航" }).click();
    }
    await page.getByRole("button", { name: "审批中心" }).click();
    await expect(page.getByRole("heading", { name: "审批中心", exact: true })).toBeVisible();
    if (username === "manager.procurement.demo" && visualOutputDir) {
      await page.screenshot({
        path: `${visualOutputDir}/07-approval-center-mobile.png`,
        fullPage: true,
      });
    }
    await logout(page);
  }
});
