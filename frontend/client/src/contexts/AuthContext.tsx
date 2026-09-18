import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { ApiError, SESSION_EXPIRED_EVENT, USING_MOCKS, getMe, logout as apiLogout } from "@/lib/api";
import type { MeResponse } from "@/lib/types";

/**
 * The one place the app learns who is signed in.
 *
 * Backed by the `["me"]` query rather than local state, so the landing navbar,
 * the auth pages and the dashboard guard all read the same cached answer
 * instead of each firing their own `/me` request.
 */
export const ME_QUERY_KEY = ["me"] as const;

interface AuthContextType {
  user: MeResponse | null;
  /** True only until the first `/me` answer arrives. */
  loading: boolean;
  /** The backend could not be reached at all -- distinct from "signed out". */
  unreachable: boolean;
  refreshUser: () => Promise<MeResponse | null>;
  /** Ends the session and leaves the dashboard for `redirectTo` (default: home). */
  logout: (redirectTo?: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

/** `/me` answers 401 for a signed-out visitor; that is a normal result, not an error. */
async function fetchMe(): Promise<MeResponse | null> {
  try {
    return await getMe();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const { data, isPending, isError } = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: fetchMe,
    // Mock mode has no backend session to ask about.
    enabled: !USING_MOCKS,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    // The app-wide default keeps previous data during refetches, which would
    // keep showing a signed-out user as signed in after their session ends.
    placeholderData: undefined,
  });

  useEffect(() => {
    // Any authenticated request that comes back 401 means the session is gone.
    const onSessionExpired = () => queryClient.setQueryData(ME_QUERY_KEY, null);
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
  }, [queryClient]);

  const refreshUser = useCallback(
    () => queryClient.fetchQuery({ queryKey: ME_QUERY_KEY, queryFn: fetchMe, staleTime: 0 }),
    [queryClient],
  );

  const logout = useCallback(async (redirectTo = "/") => {
    try {
      await apiLogout();
    } catch {
      // Even if the backend call fails, the local session view must end.
    }
    // Leave the dashboard in the same tick the user is cleared, so the guard
    // never sees "signed out on /dashboard" and redirects to /auth instead.
    navigate(redirectTo, { replace: true });
    // Update ["me"] in place, never remove it: `queryClient.clear()` detached
    // the live observer, which kept serving the old user, so /auth bounced
    // straight back to /dashboard.
    queryClient.setQueryData(ME_QUERY_KEY, null);
    // Drop every other cached read so the next account never sees this one's data.
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== ME_QUERY_KEY[0] });
  }, [navigate, queryClient]);

  const value = useMemo<AuthContextType>(
    () => ({
      user: data ?? null,
      loading: !USING_MOCKS && isPending,
      unreachable: isError,
      refreshUser,
      logout,
    }),
    [data, isPending, isError, refreshUser, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
