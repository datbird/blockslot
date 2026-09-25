/**
 * Controls that survive Steam's on-screen keyboard.
 *
 * While the virtual keyboard is up, the click that follows a press is
 * retargeted to whatever is underneath, so React's onClick never runs and the
 * button looks dead. Proven on this Deck, not inferred. So every button here
 * listens for BOTH pointerdown and click, behind a timestamp guard that
 * collapses the two events of one ordinary press into one activation.
 *
 * DFL's types also describe what DFL hopes Steam's components accept, not what
 * they honour. `onKeyDown` on a TextField is dropped, so Enter is caught on a
 * wrapper in the capture phase instead.
 */

import { DialogButton, TextField } from "@decky/ui";
import { FC, ReactNode, useRef } from "react";

const GUARD_MS = 500;

export const SafeButton: FC<{
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
  style?: any;
  children?: ReactNode;
}> = ({ onClick, disabled, primary, style, children }) => {
  // A press, fired once, whether the click or the pointerdown gets through.
  const last = useRef(0);
  const fire = () => {
    const now = Date.now();
    if (disabled || now - last.current < GUARD_MS) return;
    last.current = now;
    onClick();
  };
  return (
    <DialogButton
      disabled={disabled}
      onClick={fire}
      onPointerDown={fire}
      style={{
        ...(primary ? { background: "#1f6feb" } : {}),
        ...(style || {}),
      }}
    >
      {children}
    </DialogButton>
  );
};

/** A text field that reports Enter, which DFL's own prop does not deliver. */
export const Text: FC<{
  label: string;
  value: string;
  onChange: (value: string) => void;
  onEnter?: () => void;
  password?: boolean;
  description?: string;
}> = ({ label, value, onChange, onEnter, password, description }) => (
  <div
    onKeyDownCapture={(event: any) => {
      if (event?.key === "Enter" && onEnter) onEnter();
    }}
  >
    <TextField
      label={label}
      description={description}
      value={value}
      bIsPassword={password}
      onChange={(event: any) => onChange(event.target.value)}
    />
  </div>
);
