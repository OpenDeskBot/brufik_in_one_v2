"""提醒轮询的稳定入口；调度声明不随文件路径改变。"""

from deskbot_server.schedule.executor import scheduled


@scheduled("reminders.poll", "poll_interval_sec")
async def poll(service):
    await service._tick()
