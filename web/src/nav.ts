import { Activity, BarChart3, History, LayoutDashboard, ScrollText, Settings, ShieldCheck, WalletCards, type LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  /** Short label for the phone bottom bar. */
  short: string;
  icon: LucideIcon;
  admin?: boolean;
  /** Shown in the phone bottom bar (the rest live in the search palette and the user menu). */
  primary?: boolean;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Overview", short: "Home", icon: LayoutDashboard, primary: true },
  { to: "/trading", label: "Paper Trading", short: "Trade", icon: WalletCards, primary: true },
  { to: "/history", label: "History", short: "History", icon: History, primary: true },
  { to: "/rrg", label: "RRG", short: "RRG", icon: BarChart3, primary: true },
  { to: "/quality", label: "Data Quality", short: "Quality", icon: Activity, primary: true },
  { to: "/logs", label: "Logs", short: "Logs", icon: ScrollText, admin: true },
  { to: "/settings", label: "Settings", short: "Settings", icon: Settings, admin: true },
];

export const ADMIN_ICON = ShieldCheck;

export function titleFor(pathname: string): string {
  const item = NAV.find((n) => (n.to === "/" ? pathname === "/" : pathname.startsWith(n.to)));
  return item ? item.label : "Page not found";
}
