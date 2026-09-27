'use client';

import { createContext, useContext, useState, ReactNode, useCallback, useEffect } from 'react';
import { authApi } from '@/lib/api';
import { User, TokenResponse } from '@/lib/types';

interface AuthContextType {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, full_name?: string) => Promise<void>;
  logout: () => void;
  updateUser: (user: User) => void;
}

const AUTH_TOKEN_KEY = 'raivisox_token';
const AUTH_USER_KEY = 'raivisox_user';

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Restore the session on first mount (page refresh).
  useEffect(() => {
    const storedToken = localStorage.getItem(AUTH_TOKEN_KEY);
    if (!storedToken) return;
    setToken(storedToken);
    authApi
      .getMe(storedToken)
      .then((me) => {
        setUser(me);
        localStorage.setItem(AUTH_USER_KEY, JSON.stringify(me));
      })
      .catch(() => {
        // Token invalid/expired — clear the stored session.
        localStorage.removeItem(AUTH_TOKEN_KEY);
        localStorage.removeItem(AUTH_USER_KEY);
        setToken(null);
      });
  }, []);

  const persistSession = (newToken: string, me: User) => {
    setToken(newToken);
    setUser(me);
    localStorage.setItem(AUTH_TOKEN_KEY, newToken);
    localStorage.setItem(AUTH_USER_KEY, JSON.stringify(me));
  };

  const login = useCallback(async (email: string, password: string) => {
    setIsLoading(true);
    try {
      const data: TokenResponse = await authApi.login(email, password);
      const me = await authApi.getMe(data.access_token);
      persistSession(data.access_token, me);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const register = useCallback(async (email: string, password: string, full_name?: string) => {
    setIsLoading(true);
    try {
      await authApi.register(email, password, full_name);
      // Auto-login after successful registration.
      const data: TokenResponse = await authApi.login(email, password);
      const me = await authApi.getMe(data.access_token);
      persistSession(data.access_token, me);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    setToken(null);
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_USER_KEY);
  }, []);

  const updateUser = useCallback((updatedUser: User) => {
    setUser(updatedUser);
  }, []);

  return (
    <AuthContext.Provider value={{ user, token, isLoading, login, register, logout, updateUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
