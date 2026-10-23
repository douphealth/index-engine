import { clearSessionCookie } from "../../../lib/auth.js";
import { json } from "../../../lib/http.js";

export const onRequestPost = () => json({ ok: true }, 200, { "Set-Cookie": clearSessionCookie() });
