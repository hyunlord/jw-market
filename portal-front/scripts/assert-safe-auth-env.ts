export function assertSafePortalAuthEnv(
  env: Record<string, string | undefined>,
): void {
  if (env.TEST_LOGIN_BYPASS === 'true') {
    throw new Error(
      'TEST_LOGIN_BYPASS=true is forbidden; use GenOS IAM login',
    )
  }
}
