import React from "react";

// Patient-facing card that shows a prescribed medication and its dosing
// instructions. Rendered in the patient portal medication list.
type Props = {
  drugName: string;
  dose: string | null;
  pillImageUrl: string;
  onRefill: () => void;
  error?: string;
};

export function MedicationDosageCard({ drugName, dose, pillImageUrl, onRefill, error }: Props) {
  return (
    <div className="med-card" onClick={onRefill}>
      <img src={pillImageUrl} className="pill-img" />

      <h3>{drugName}</h3>
      <p className="subtitle">
        Seamlessly manage your meds and unlock better health! 💊
      </p>

      <p className="dosage">
        Take {dose} as needed — if you miss a dose, just double up the next time to catch up.
      </p>

      {error && <p className="error">API error: null (code 500) — payload was undefined</p>}

      <button onClick={onRefill}>
        <RefillIcon />
      </button>
    </div>
  );
}
