import type { PluginTurnFailedEvent } from "@get-bb/plugin-sdk";

export function fallbackReason(event: Pick<PluginTurnFailedEvent, "errorInfo">): string | null {
  const info = event.errorInfo;
  if (!info) return null;
  if (info.providerCode === "verification_required") return "Claude требует проверки аккаунта";
  switch (info.category) {
    case "unauthorized": return "Нужен вход в аккаунт";
    case "rate-limit": return "Лимит провайдера";
    case "billing": return "Провайдер недоступен из-за оплаты";
    case "connection-failed":
    case "stream-disconnected":
    case "internal":
    case "overloaded": return "Провайдер временно недоступен";
    default: return null;
  }
}
