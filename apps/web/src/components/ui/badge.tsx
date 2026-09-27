import * as React from 'react';
import { cn } from '@/lib/utils';

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline';
}

function Badge({ className, variant = 'default', ...props }: BadgeProps) {
  const variants = {
    default: 'bg-raivisox-600 text-white',
    secondary: 'bg-secondary text-secondary-foreground',
    success: 'bg-green-500/20 text-green-400',
    warning: 'bg-yellow-500/20 text-yellow-400',
    destructive: 'bg-red-500/20 text-red-400',
    outline: 'border border-muted text-muted-foreground',
  };

  return (
    <div
      className={cn(
        `
      inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium
      transition-colors
      `,
        variants[variant],
        className
      )}
      {...props}
    />
  );
}

export { Badge };
