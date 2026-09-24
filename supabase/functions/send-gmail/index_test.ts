import { assertEquals } from "jsr:@std/assert@1"

import { smtpFailure } from "./index.ts"


Deno.test("classifies an invalid recipient as a hard bounce", () => {
  const error = Object.assign(new Error("550 5.1.1 user unknown"), {
    responseCode: 550,
    response: "550 5.1.1 user unknown",
  })
  const result = smtpFailure(error)
  assertEquals(result.hardBounce, true)
  assertEquals(result.retryable, false)
  assertEquals(result.failureType, "hard_bounce")
  assertEquals(result.status, 422)
})

Deno.test("classifies throttling as a temporary failure", () => {
  const error = Object.assign(new Error("421 4.7.0 temporary rate limit"), {
    responseCode: 421,
  })
  const result = smtpFailure(error)
  assertEquals(result.hardBounce, false)
  assertEquals(result.retryable, true)
  assertEquals(result.failureType, "temporary")
  assertEquals(result.status, 503)
})

Deno.test("classifies bad Gmail credentials as permanent, not bounced", () => {
  const error = Object.assign(new Error("535 5.7.8 Username and Password not accepted"), {
    code: "EAUTH",
    responseCode: 535,
  })
  const result = smtpFailure(error)
  assertEquals(result.hardBounce, false)
  assertEquals(result.retryable, false)
  assertEquals(result.failureType, "permanent")
  assertEquals(result.status, 401)
})
