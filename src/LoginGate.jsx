/**
 * LoginGate.jsx — nothing on the dashboard renders until a trading admin signs in.
 *
 * This is the front door, not the security boundary: private_access_schema.sql makes the
 * database refuse the public key, so skipping this screen shows an empty page. Sign-in is a
 * one-time email link for accounts that already exist. The form never creates one, and it
 * answers the same way for any email, so it can't be used to learn who has access.
 */
import { useEffect, useState } from "react";
import { authClient, useAdminSession, RUN_CSS } from "./RunPipeline";
import { CSS } from "./Dashboard";

const GATE_CSS = `
  .gate{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;background:var(--bg);}
  .gate-card{width:100%;max-width:400px;background:var(--bg2);border:1px solid var(--border-amber);border-radius:6px;padding:24px;display:flex;flex-direction:column;gap:12px;}
  .gate-card .logo{margin:0;}
`;

function Shell({ children }) {
  return (
    <>
      <style>{CSS + RUN_CSS + GATE_CSS}</style>
      <main className="gate">
        <div className="gate-card">
          <div className="logo">Orblex <span>/ Industries</span></div>
          {children}
        </div>
      </main>
    </>
  );
}

function SignIn() {
  const [email, setEmail] = useState("");
  const [sent, setSent]   = useState(false);
  const [busy, setBusy]   = useState(false);
  const [error, setError] = useState(null);

  const send = async e => {
    e.preventDefault();
    setBusy(true); setError(null);
    const { error: err } = await authClient.auth.signInWithOtp({
      email: email.trim(),
      options: { shouldCreateUser: false, emailRedirectTo: window.location.origin },
    });
    setBusy(false);
    // An unknown email is reported as sent: only a rate limit or outage shows as an error.
    if (err && err.status !== 400 && err.status !== 422) setError("Couldn't send the link. Wait a minute and try again.");
    else setSent(true);
  };

  if (sent) return (
    <Shell>
      <p className="run-p">If <b>{email}</b> has access, a sign-in link is on its way. Open it in this browser.</p>
      <button className="run-secondary" onClick={() => setSent(false)}>USE A DIFFERENT EMAIL</button>
    </Shell>
  );
  return (
    <Shell>
      <p className="run-p">This dashboard is private. Sign in with your email and we'll send you a one-time link.</p>
      <form onSubmit={send} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <label className="run-label" htmlFor="gate-email">EMAIL</label>
        <input id="gate-email" className="run-input" type="email" required autoComplete="email" autoFocus
               value={email} onChange={e => setEmail(e.target.value)} />
        <button className="run-primary" type="submit" disabled={busy || !email.trim()}>
          {busy ? "SENDING…" : "SEND SIGN-IN LINK"}
        </button>
      </form>
      {error && <div className="run-error" style={{ margin: 0 }} role="alert">{error}</div>}
    </Shell>
  );
}

export default function LoginGate({ children }) {
  const session = useAdminSession();
  const [admin, setAdmin] = useState(null);       // null = not checked yet
  const uid = session?.user?.id;

  useEffect(() => {
    if (!uid) { setAdmin(null); return; }
    let live = true;
    authClient.rpc("_is_trading_admin").then(({ data, error }) => { if (live) setAdmin(!error && data === true); });
    return () => { live = false; };
  }, [uid]);

  if (session === undefined || (session && admin === null)) return <Shell><p className="run-p">Checking your sign-in…</p></Shell>;
  if (!session) return <SignIn />;
  if (!admin) return (
    <Shell>
      <p className="run-p"><b>{session.user?.email}</b> doesn't have access to this dashboard.</p>
      <button className="run-secondary" onClick={() => authClient.auth.signOut()}>SIGN OUT</button>
    </Shell>
  );
  return children;
}
