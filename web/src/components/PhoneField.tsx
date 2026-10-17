import { useId } from "react";
import { COUNTRIES, buildE164, formatPreview } from "../lib/phone";
import { Select } from "../ui/fields";

interface Props {
  country: string;
  national: string;
  onCountry: (code: string) => void;
  onNational: (value: string) => void;
  error?: string | null;
}

/** Country code and number as two controls, so nobody has to know the "+91" convention. */
export function PhoneField({ country, national, onCountry, onNational, error }: Props) {
  const id = useId();
  const e164 = buildE164(country, national);
  const hintId = `${id}-hint`;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor={`${id}-number`} className="text-sm font-medium text-fg">
        Mobile number
      </label>
      <div className="flex gap-2">
        <Select aria-label="Country code" value={country} onChange={(e) => onCountry(e.target.value)} className="w-28 shrink-0">
          {COUNTRIES.map((c) => (
            <option key={c.code} value={c.code}>
              +{c.code} {c.name}
            </option>
          ))}
        </Select>
        <input
          id={`${id}-number`}
          type="tel"
          inputMode="tel"
          autoComplete="tel-national"
          required
          value={national}
          onChange={(e) => onNational(e.target.value)}
          aria-describedby={hintId}
          aria-invalid={error ? true : undefined}
          className="num min-h-10 w-full min-w-0 rounded-control border border-control bg-surface px-3 text-sm text-left text-fg placeholder:text-subtle transition-colors hover:border-fg/40 aria-[invalid=true]:border-neg"
          placeholder="98765 43210"
        />
      </div>
      <p id={hintId} className={error ? "text-xs font-medium text-neg" : "text-xs text-muted"} role={error ? "alert" : undefined}>
        {error ?? (e164 ? `We will text a code to ${formatPreview(e164, country)}.` : "We will text you a verification code.")}
      </p>
    </div>
  );
}
