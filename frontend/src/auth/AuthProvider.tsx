import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getCurrentUser, login as loginRequest, logout as logoutRequest, type CurrentUser } from "../api/client";

type AuthState = {
  user: CurrentUser | null;
  restoring: boolean;
  login(username: string, password: string): Promise<void>;
  logout(): Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [restoring, setRestoring] = useState(true);
  useEffect(() => {
    void getCurrentUser()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setRestoring(false));
  }, []);
  useEffect(() => {
    const expire = () => setUser(null);
    window.addEventListener("policy-session-expired", expire);
    return () => window.removeEventListener("policy-session-expired", expire);
  }, []);
  const value = useMemo<AuthState>(() => ({
    user, restoring,
    async login(username, password) {
      await loginRequest(username, password);
      setUser(await getCurrentUser() ?? { username, role: "employee" });
    },
    async logout() { await logoutRequest(); setUser(null); },
  }), [user, restoring]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider is required");
  return value;
}
