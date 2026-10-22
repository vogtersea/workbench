package com.worklog.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/** 开机后重排每日提醒——系统重启会清空所有闹钟。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Log.i("Worklog", "开机完成，重排每日提醒");
        ReminderScheduler.schedule(context);
    }
}
