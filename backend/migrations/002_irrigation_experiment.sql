CREATE TABLE IF NOT EXISTS irrigation_experiment (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code text NOT NULL UNIQUE,
    run_id integer NOT NULL REFERENCES climate_run(id),
    starts_at timestamptz NOT NULL,
    delta_minutes integer NOT NULL CHECK (delta_minutes BETWEEN 1 AND 30),
    state text NOT NULL CHECK (state IN ('active', 'voided')),
    void_reason text,
    actor text NOT NULL REFERENCES account(username),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS experiment_id integer REFERENCES irrigation_experiment(id);
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS arm text CHECK (arm IN ('control', 'treatment'));
CREATE UNIQUE INDEX IF NOT EXISTS one_arm_per_experiment ON irrigation_round (experiment_id, arm) WHERE experiment_id IS NOT NULL;
