ALTER TABLE parishes ADD COLUMN IF NOT EXISTS subscription_plan VARCHAR(20);

ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS payment_date TIMESTAMP;
ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS coverage_start TIMESTAMP;
ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS coverage_end TIMESTAMP;
ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50);
ALTER TABLE payment_submissions ADD COLUMN IF NOT EXISTS notes TEXT;

UPDATE parishes
SET subscription_plan = latest.plan
FROM (
    SELECT DISTINCT ON (parish_id) parish_id, plan
    FROM payment_submissions
    WHERE status = 'confirmed'
    ORDER BY parish_id, confirmed_at DESC NULLS LAST, id DESC
) AS latest
WHERE parishes.id = latest.parish_id
  AND parishes.subscription_plan IS NULL
  AND parishes.plan = 'active';
