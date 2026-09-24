import nodemailer from "npm:nodemailer@^7.0.6"

type SendPayload = {
  to?: string
  subject?: string
  body?: string
  content_type?: "plain" | "html"
  from_name?: string
  reply_to?: string
  account_id?: string
  gmail_user?: string
  gmail_app_password?: string
  message_id?: string
  in_reply_to?: string
  references?: string
  verify_only?: boolean
}

type SmtpError = Error & {
  code?: string
  response?: string
  responseCode?: number
  command?: string
}

const HARD_BOUNCE_MARKERS = [
  "5.1.1",
  "5.1.0",
  "user unknown",
  "no such user",
  "unknown recipient",
  "invalid recipient",
  "recipient address rejected",
  "address does not exist",
  "mailbox not found",
  "mailbox unavailable",
  "mailbox disabled",
]

export function smtpFailure(error: unknown) {
  const smtp = error as Partial<SmtpError>
  const message = error instanceof Error ? error.message : String(error)
  const response = `${smtp.response ?? ""} ${message}`.trim()
  const parsedCode = Number(response.match(/\b([245]\d\d)\b/)?.[1] ?? 0)
  const smtpCode = Number(smtp.responseCode ?? parsedCode)
  const normalized = response.toLowerCase()
  const authFailure = smtp.code === "EAUTH" || smtpCode === 535 ||
    normalized.includes("invalid login") || normalized.includes("authentication failed") ||
    normalized.includes("username and password not accepted")
  const hardBounce = smtpCode >= 500 && smtpCode < 600 &&
    HARD_BOUNCE_MARKERS.some((marker) => normalized.includes(marker))
  const retryable = !hardBounce && !authFailure &&
    (smtpCode === 0 || (smtpCode >= 400 && smtpCode < 500))
  return {
    message,
    smtpCode: smtpCode || null,
    hardBounce,
    retryable,
    failureType: hardBounce ? "hard_bounce" : retryable ? "temporary" : "permanent",
    status: retryable ? 503 : authFailure ? 401 : 422,
  }
}

const accounts = {
  "1": {
    user: (Deno.env.get("GMAIL_USER") ?? "").trim(),
    password: (Deno.env.get("GMAIL_APP_PASSWORD") ?? "").replaceAll(" ", "").trim(),
  },
  "2": {
    user: (Deno.env.get("GMAIL_USER_2") ?? "").trim(),
    password: (Deno.env.get("GMAIL_APP_PASSWORD_2") ?? "").replaceAll(" ", "").trim(),
  },
}

function transportFor(user: string, password: string) {
  return nodemailer.createTransport({
    host: "smtp.gmail.com",
    port: 465,
    secure: true,
    auth: { user, pass: password },
    connectionTimeout: 20_000,
    greetingTimeout: 20_000,
    socketTimeout: 30_000,
  })
}

function plainText(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .trim()
}

export default {
  async fetch(req: Request): Promise<Response> {
    if (req.method !== "POST") {
      return Response.json({ ok: false, error: "Method not allowed" }, { status: 405 })
    }
    try {
      const payload = (await req.json()) as SendPayload
      const accountId = (payload.account_id ?? "1").trim()
      const fallback = accounts[accountId as keyof typeof accounts]
      const account = {
        user: (payload.gmail_user ?? fallback?.user ?? "").trim(),
        password: (payload.gmail_app_password ?? fallback?.password ?? "").replaceAll(" ", "").trim(),
      }
      if (!account.user || !account.password) {
        return Response.json({
          ok: false,
          error: `Gmail account ${accountId} credentials are missing`,
          retryable: false,
          hard_bounce: false,
          failure_type: "configuration",
        }, { status: 422 })
      }
      const transport = transportFor(account.user, account.password)
      if (payload.verify_only) {
        await transport.verify()
        return Response.json({ ok: true, account_id: accountId })
      }
      const to = (payload.to ?? "").trim()
      const subject = (payload.subject ?? "").trim()
      const body = payload.body ?? ""
      if (!to || !to.includes("@") || !subject || !body) {
        return Response.json({
          ok: false,
          error: "to, subject, and body are required",
          retryable: false,
          hard_bounce: false,
          failure_type: "validation",
        }, { status: 400 })
      }

      const fromName = (payload.from_name ?? "Outreach").replace(/[\r\n"]/g, "").trim()
      const replyTo = (payload.reply_to ?? account.user).trim()
      const isHtml = payload.content_type === "html"
      const info = await transport.sendMail({
        from: `"${fromName || "Outreach"}" <${account.user}>`,
        to,
        replyTo,
        subject,
        text: isHtml ? plainText(body) : body,
        html: isHtml ? body : undefined,
        messageId: payload.message_id || undefined,
        inReplyTo: payload.in_reply_to || undefined,
        references: payload.references ? payload.references.split(/\s+/).filter(Boolean) : undefined,
      })
      return Response.json({ ok: true, message_id: info.messageId, account_id: accountId })
    } catch (error) {
      const failure = smtpFailure(error)
      console.error("Gmail delivery failed", {
        failure_type: failure.failureType,
        smtp_code: failure.smtpCode,
      })
      return Response.json({
        ok: false,
        error: failure.message,
        retryable: failure.retryable,
        hard_bounce: failure.hardBounce,
        failure_type: failure.failureType,
        smtp_code: failure.smtpCode,
      }, { status: failure.status })
    }
  },
}
