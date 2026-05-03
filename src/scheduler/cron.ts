/** Minimal cron expression parser — computes the next fire date from a cron string.
 *
 * Supports standard 5-field cron: minute hour day-of-month month day-of-week
 * Does NOT support seconds, years, or non-standard extensions.
 */

interface CronFields {
	minutes: Set<number>;
	hours: Set<number>;
	daysOfMonth: Set<number>;
	months: Set<number>;
	daysOfWeek: Set<number>;
}

function parseCronNumber(value: string): number | null {
	if (!/^\d+$/.test(value)) return null;
	const parsed = Number.parseInt(value, 10);
	return Number.isInteger(parsed) ? parsed : null;
}

function parseRange(
	range: string,
	min: number,
	max: number,
): [number, number] | null {
	if (range === "*") return [min, max];
	if (range.includes("-")) {
		const [startStr, endStr] = range.split("-");
		if (!startStr || !endStr) return null;
		const start = parseCronNumber(startStr);
		const end = parseCronNumber(endStr);
		if (start == null || end == null || start > end) return null;
		return [start, end];
	}

	const start = parseCronNumber(range);
	if (start == null) return null;
	return [start, max];
}

function addCronValue(
	values: Set<number>,
	value: number,
	min: number,
	max: number,
): boolean {
	if (!Number.isInteger(value) || value < min || value > max) return false;
	values.add(value);
	return true;
}

/** Parse a single cron field into a set of valid values */
function parseField(
	field: string,
	min: number,
	max: number,
): Set<number> | null {
	const values = new Set<number>();

	for (const part of field.split(",")) {
		if (part === "*") {
			for (let i = min; i <= max; i++) values.add(i);
		} else if (part.includes("/")) {
			const [range, stepStr] = part.split("/");
			if (!range || !stepStr) return null;
			const step = parseCronNumber(stepStr);
			const parsedRange = parseRange(range, min, max);
			if (step == null || step <= 0 || !parsedRange) return null;
			const [start, end] = parsedRange;
			for (let i = start; i <= end; i += step) {
				if (!addCronValue(values, i, min, max)) return null;
			}
		} else if (part.includes("-")) {
			const parsedRange = parseRange(part, min, max);
			if (!parsedRange) return null;
			const [start, end] = parsedRange;
			for (let i = start; i <= end; i++) {
				if (!addCronValue(values, i, min, max)) return null;
			}
		} else {
			const value = parseCronNumber(part);
			if (value == null || !addCronValue(values, value, min, max)) {
				return null;
			}
		}
	}

	return values.size > 0 ? values : null;
}

/** Parse a 5-field cron expression */
function parseCron(expression: string): CronFields | null {
	const parts = expression.trim().split(/\s+/);
	if (parts.length !== 5) return null;
	const [minutes, hours, daysOfMonth, months, daysOfWeek] = parts;
	if (!minutes || !hours || !daysOfMonth || !months || !daysOfWeek) return null;

	const parsed = {
		minutes: parseField(minutes, 0, 59),
		hours: parseField(hours, 0, 23),
		daysOfMonth: parseField(daysOfMonth, 1, 31),
		months: parseField(months, 1, 12),
		daysOfWeek: parseField(daysOfWeek, 0, 6), // 0 = Sunday
	};
	if (
		!parsed.minutes ||
		!parsed.hours ||
		!parsed.daysOfMonth ||
		!parsed.months ||
		!parsed.daysOfWeek
	) {
		return null;
	}

	return parsed as CronFields;
}

/** Compute the next fire date from a cron expression, starting from now */
export function parseCronToNextDate(expression: string): Date | null {
	const fields = parseCron(expression);
	if (!fields) return null;

	const now = new Date();
	const candidate = new Date(now);
	candidate.setSeconds(0, 0);
	candidate.setMinutes(candidate.getMinutes() + 1); // Start from next minute

	// Search up to 366 days ahead
	const maxIterations = 366 * 24 * 60;
	for (let i = 0; i < maxIterations; i++) {
		const month = candidate.getMonth() + 1; // 1-12
		const dayOfMonth = candidate.getDate();
		const dayOfWeek = candidate.getDay(); // 0-6, 0=Sunday
		const hour = candidate.getHours();
		const minute = candidate.getMinutes();

		if (
			fields.months.has(month) &&
			fields.daysOfMonth.has(dayOfMonth) &&
			fields.daysOfWeek.has(dayOfWeek) &&
			fields.hours.has(hour) &&
			fields.minutes.has(minute)
		) {
			return candidate;
		}

		candidate.setMinutes(candidate.getMinutes() + 1);
	}

	return null; // No match found within a year
}

/** Validate a cron expression — returns null if valid, error message if not */
export function validateCron(expression: string): string | null {
	const parts = expression.trim().split(/\s+/);
	if (parts.length !== 5) {
		return "Cron expression must have 5 fields: minute hour day month weekday";
	}

	const fields = parseCron(expression);
	if (!fields) return "Invalid cron expression";

	// Check minimum interval (1 hour)
	if (fields.minutes.size === 60 && fields.hours.size === 24) {
		return "Interval too short — minimum 1 hour between recurring reminders";
	}

	return null;
}
