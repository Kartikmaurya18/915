import type { ReactNode } from "react";
import { AuthProvider } from "../src/auth/AuthContext";
import { SelectionProvider } from "../src/state/SelectionContext";
import { ThemeProvider } from "../src/theme/ThemeProvider";
import { UIProvider } from "../src/ui/UIProvider";

/** The same provider stack as src/main.tsx (the router is supplied by each test). */
export function AllProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <AuthProvider>
        <SelectionProvider>
          <UIProvider>{children}</UIProvider>
        </SelectionProvider>
      </AuthProvider>
    </ThemeProvider>
  );
}
