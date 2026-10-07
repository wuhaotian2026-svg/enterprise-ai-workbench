export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

export async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const csrf = document.cookie
    .split("; ")
    .find((item) => item.startsWith("policy_csrf="))
    ?.split("=")[1];
  const mutating = init.method
    ? !["GET", "HEAD", "OPTIONS"].includes(init.method.toUpperCase())
    : false;
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(mutating && csrf
        ? { "X-CSRF-Token": decodeURIComponent(csrf) }
        : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body: Record<string, unknown> = await response
      .json()
      .catch(() => ({ code: "request_failed" }));
    if (response.status === 401 && path !== "/auth/login") {
      window.dispatchEvent(new Event("policy-session-expired"));
    }
    const code = typeof body.code === "string" ? body.code : "request_failed";
    throw new ApiError(response.status, code, body);
  }
  return response;
}
