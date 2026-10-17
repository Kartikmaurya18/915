import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Splash } from "../components/guards";

/**
 * Where the server sends the browser after a successful provider sign-in. The server has already set the HttpOnly
 * refresh cookie; this page trades it for an access token (kept in memory, exactly like a password sign-in) and
 * moves on. Nothing sensitive was in the address that led here.
 */
export default function OAuthComplete() {
  const { status, completeOAuth } = useAuth();
  const started = useRef(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void completeOAuth().then((ok) => setFailed(!ok));
  }, [completeOAuth]);

  if (status === "authenticated") return <Navigate to="/" replace />;
  if (failed) return <Navigate to="/login?error=oauth_failed" replace />;
  return <Splash message="Signing you in…" />;
}
