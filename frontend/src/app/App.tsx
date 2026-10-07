import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { LoginPage } from "../auth/LoginPage";
import { AppShell } from "./AppShell";

function AppContent() {
  const { user, restoring, logout } = useAuth();
  if (restoring) return <main className="restore-screen" aria-live="polite"><span>正在核验会话</span></main>;
  if (!user) return <LoginPage />;
  return <AppShell user={user} onLogout={logout} />;
}

export function App() { return <AuthProvider><AppContent /></AuthProvider>; }
