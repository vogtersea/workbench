package com.worklog.app;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;
import java.util.Calendar;

/** 每日提醒的闹钟调度。用系统闹钟而非常驻线程，进程被杀也能唤醒。 */
public final class ReminderScheduler {

    private static final String TAG = "Worklog";
    public static final int REQUEST_CODE = 1001;
    /** 默认提醒时间：每天 09:00 */
    public static final int HOUR = 9;
    public static final int MINUTE = 0;

    private ReminderScheduler() { }

    public static void schedule(Context context) {
        AlarmManager manager = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (manager == null) {
            return;
        }
        PendingIntent pending = pendingIntent(context);
        Calendar next = Calendar.getInstance();
        next.set(Calendar.HOUR_OF_DAY, HOUR);
        next.set(Calendar.MINUTE, MINUTE);
        next.set(Calendar.SECOND, 0);
        next.set(Calendar.MILLISECOND, 0);
        if (next.getTimeInMillis() <= System.currentTimeMillis()) {
            next.add(Calendar.DAY_OF_YEAR, 1);
        }
        long triggerAt = next.getTimeInMillis();
        boolean exact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || manager.canScheduleExactAlarms();
        try {
            if (exact) {
                manager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending);
            } else {
                // 无精确闹钟权限时退化为不精确，但仍能在低电耗模式下唤醒
                manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pending);
            }
            Log.i(TAG, "每日提醒已排程: " + next.getTime() + " exact=" + exact);
        } catch (SecurityException e) {
            Log.w(TAG, "排程失败（缺权限）", e);
        }
    }

    public static PendingIntent pendingIntent(Context context) {
        Intent intent = new Intent(context, ReminderReceiver.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        return PendingIntent.getBroadcast(context, REQUEST_CODE, intent, flags);
    }
}
