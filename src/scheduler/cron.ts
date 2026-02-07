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

/** Parse a single cron field into a set of valid values */
function parseField(field: string, min: number, max: number): Set<number> {
	const values = new Set<number>();

	for (const part of field.split(",")) {
		if (part === "*") {
			for (let i = min; i <= max; i++) values.add(i);
		} else if (part.includes("/")) {
			const [range, stepStr] = part.split("/");
			const step = Number.parseInt(stepStr!, 10);
			const start = range === "*" ? min : Number.parseInt(range!, 10);
			for (let i = start; i <= max; i += step) values.add(i);
		} else if (part.includes("-")) {
			const [startStr, endStr] = part.split("-");
			const start = Number.parseInt(startStr!, 10);
			const end = Number.parseInt(endStr!, 10);
			for (let i = start; i <= end; i++) values.add(i);
		} else {
			values.add(Number.parseInt(part, 10));
		}
	}

	return values;
}

/** Parse a 5-field cron expression */
function parseCron(expression: string): CronFields | null {
	const parts = expression.trim().split(/\s+/);
	if (parts.length !== 5) return null;

	return {
		minutes: parseField(parts[0]!, 0, 59),
		hours: parseField(parts[1]!, 0, 23),
		daysOfMonth: parseField(parts[2]!, 1, 31),
		months: parseField(parts[3]!, 1, 12),
		daysOfWeek: parseField(parts[4]!, 0, 6), // 0 = Sunday
	};
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
