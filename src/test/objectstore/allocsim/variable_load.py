#!/bin/python
import rados
import time
import math
import threading
import argparse
import logging
import random

logger = logging.getLogger("iots")

"""
Workloads are defined based on sinusiodal waves.

The base is a definition of workload density
  D(x) = 1 + sin(x)
where x defines a point in time.
The period of function D is 2pi.

The amount of IOs to execute in workload W until time t is:
  W(t) = Int(0, t){x + cos(x)}

The algorithm keeps calculating W(t) and schedules the additional IOs.

Actual useful form includes amplitiude(A), frequency(F) and shift(S)
  D(x) = A * (1 + sin(F * x + S))
Which translates into:
  Int(D) = A * (x + cos(F * x + S)/F)
  W(t) = Int(0,t){D} = D(t) - D(0)

To get workloads that go sometimes completely mute, one can shift
values of workload density by constant value M.
  D(x) = A * (1 + sin(F*x+S) - M)

At M<0    : D(x)>0, can be used to produce constant shift to workload density
At M=0    : D(x)>=0, basic sinusoidal workload that never stops
M in (0,1): D(x) gets both + and - values; for large t, W(t)->+inf, ideal to model sporadic bursts of load
M in <1,2>: W(t) can be possitive for small t, but for large t, W(t)->-inf; it is not useful
At M>=2   : D(x)<=0, produces no workload

  Int(D) = A * (x - cos(F*x+S)/F - M*x) =
         = A * ((1-M)*x - cos(F*x+S)/F)
This variant of D(x) can achieve negative values, and as result W(t) decreases.
The algorithm stops scheduling new IOs when W(t) decreases until W(t) rises above
previous maximum value.

M is replaced with coefficient P that models support for W(t), that is, area where workload W(t) is doing work.
It has to work like examples:
P=0   (M=1)   : W(t) no workload, useless
P=0.3 (M=0.7) : W(t) sporadic 10% time workload occurs
P=1   (M=0)   : W(t) nicely sinusoidal workload
P=1.2 (M=-0.2): W(t) amplified workload that never reaches less then 0.2*A
The relation is: M=(1-P) P=(1-M)

  Int(D) = A * (P*x - cos(F*x+S)/F)

Workloads W(t) are sum of set of sinusoidal components
  W(t) = Int(0,t){D0 + D1 + .. +Dn} =
       = Val(0,t)( A0*(P*x-cos(F0*x+S0)/F0) + ... + An*(P*x-cos(Fn*x+Sn)/Fn) ) =
       = Val(0,t)( Sum(Ai*P)*x - Sum(Ai*cos(Fi*x+Si)/Fi) )
The coefficent P is defined only once per set.
"""


class Idue:
  def __init__(self) -> None:
    pass
  def due(self, x: float) -> float:
    return 0

class workload_sine(Idue):
  #set of default ranges for random to select from
  #defaut "cnt=3 amp=500-1000 period=90-120 shift=0-1 supp=1"
  default_amp: str = '500-1000'
  default_period: str = '30-60'
  default_shift: str = '0-1'
  default_supp: str = '1'
  default_cnt: int = 3
  class AFS:
    def __init__(self, A: float, F:float, S:float) -> None:
      assert(F > 0)
      self.A = A
      self.F = F
      self.S = S
      self.AdivF = A / F
  def __init__(self, params: dict) -> None:
    def construct(name: str, default: str) -> list:
      #we need to construct cnt elements from definition from params
      #input cnt=5 amp=300,10-20,500-1000
      #result: [300, 16.34, 515.93, 773.68, 604.11]
      result = list()
      #select val as either default of from param
      if name in params:
        val = params[name]
      else:
        val = default

      vals_list = val.split(',')
      #vals_list contains list of either direct values '123' or range '120-150'
      for i in range(cnt):
        #we assume there will be one element in vals_list for each element we want to produce
        #if we run out of elements in vals_list, we reuse the last one
        vals = vals_list[0].split('-')
        if len(vals_list) > 1:
          vals_list.pop(0)
        assert(0 < len(vals) and len(vals) <= 2)
        if len(vals) == 1:
          low = float(vals[0])
          high = low
        else:
          low = float(vals[0])
          high = float(vals[1])
        result.append((high-low)*rnd.random() + low)
      return result

    self.P = 0.0
    self.sum_Ai_P = 0.0
    self.afs = list()
    #first decide how many sin components are to be here
    if 'cnt' in params:
      cnt = int(params['cnt'])
    else:
      cnt = self.default_cnt
    if 'rand' in params:
      rnd = random.Random(params['rand'])
    else:
      rnd = random.Random(time.time())

    if 'supp' in params:
      self.P = float(params['supp'])
    else:
      self.P = 1

    #don't care if cnt==0, lame workload is still a workload
    amps =    construct('amp',    self.default_amp)
    periods = construct('period', self.default_period)
    shifts =  construct('shift',  self.default_shift)
    assert(len(amps) == cnt)
    assert(len(periods) == cnt)
    assert(len(shifts) == cnt)
    for i in range(cnt):
      self.afs.append(self.AFS(amps[i] / 2,
                               (2 * math.pi) / periods[i],
                               shifts[i] * (2 * math.pi) ))
      self.sum_Ai_P += amps[i] / 2 * self.P
    self.due0 = 0;
    self.due0 = self.due(0)

  def due(self, x: float) -> float:
    s = self.sum_Ai_P * x
    for i in self.afs:
      s -= i.AdivF * math.cos(i.F * x + i.S)
    return s - self.due0

class workload_custom(Idue):
  pass

class workload:
  def __init__(self, name: str, due: Idue) -> None:
    self.name = name
    self.due = due
    self.start_time = time.time()
    self.object_name = 'test_object_'
    self.object_cnt = 1000
    self.object_rr = 0
    self.iodepth = 150
    self.ops_max_backlog = 2000 # how many ops we are allowed to fall behind before dropping some
    self.ops_started = 0 # how many ops were scheduled for execution
    self.ops_done = 0 # how many ops were finished already
    self.ops_skipped = 0 # ops that should have been started, but we decided to skip them
                         # we do it when the ops backlog is already too large
    self.ops_reported = 0 # to track newly completed
    self.stopping = False
    self.stopped = False
    self.reporting_frequency = 1.0
    self.reporting_next = self.start_time

  #returns ops/s
  def work_density(self, x: float):
    return 1 + math.cos(x*0.5)
  def work_needed_until(self, x: float):
    return self.due.due(x)

  def next_op(self):
    pass
  def on_complete(self, object_name):
    def action(result):
      self.ops_done += 1
      self.schedule_ops()
      return
    return action
  #returns time.time() when next op should be scheduled
  def get_next_op_at(self) -> float:
    return next_op_at

  def schedule_op(self):
    self.ops_started += 1
    self.object_rr = (self.object_rr + 1 ) % self.object_cnt
    object_name = 'test_object_' + str(self.object_rr)
    data = b'c'*4096
    completion_context = self.on_complete(object_name)
    self.ioctx.aio_write(object_name, data, oncomplete=completion_context)

  #returns time.time() when next op should be scheduled
  def schedule_ops(self):
    if self.stopping:
      self.stopped = True
      return
    now = time.time()
    cumulative_todo = self.work_needed_until(now - self.start_time)
    if now > self.reporting_next:
      print_report(now)
    ops_to_schedule = int(cumulative_todo) - (self.ops_started + self.ops_skipped)
    # check if we have capacity to schedule more ops
    # maybe we are lagging behind
    if ops_to_schedule > self.ops_max_backlog:
      ops_to_skip = ops_to_schedule - self.ops_max_backlog
      self.ops_skipped += ops_to_skip
    #reduce if we would go above iodepth
    if ops_to_schedule + (self.ops_started - self.ops_done) > self.iodepth:
      ops_to_schedule = self.iodepth - (self.ops_started - self.ops_done)
    if ops_to_schedule >= 1:
      for i in range(math.floor(ops_to_schedule)):
        self.schedule_op()
    if self.ops_started - self.ops_done == 0:
      #signal main thread we need sleep
      self.next_op_at = time.time() + 1
      wup = threading.Timer(0.01, self.schedule_ops)
      wup.start()
    return

  def dry_schedule_ops(self):
    if self.stopping:
      self.stopped = True
      return
    now = time.time() + (time.time() - self.start_time) * 10
    cumulative_todo = self.work_needed_until(now - self.start_time)
    if now > self.reporting_next:
      print_report(now)
    ops_to_schedule = int(cumulative_todo) - (self.ops_started + self.ops_skipped)
    self.ops_started += ops_to_schedule
    self.ops_done += ops_to_schedule
    wup = threading.Timer(0.01, self.dry_schedule_ops)
    wup.start()
    return

  def start(self, ioctx: rados.Ioctx):
    self.ioctx = ioctx
    self.schedule_ops()

  def dry_start(self):
    self.dry_schedule_ops()

  def stop(self):
    self.stopping = True
    while (not self.stopped or self.ops_done != self.ops_started):
      logger.debug("stopped=%d ios_in_flight=%d" % \
                   (self.stopped, self.ops_started - self.ops_done))
      time.sleep(0.01)
    logger.debug("workload %s stopped" % self.name)

#end workload class

def print_report(now):
  elapsed = now - workloads[0].start_time
  iops = 0
  for w in workloads:
    iops = iops + (w.ops_done - w.ops_reported)
    w.reporting_next = w.reporting_next + w.reporting_frequency
    w.ops_reported = w.ops_done
  print("time=%7.3f" % elapsed + \
        " iops=%d" % iops)

def run(workloads, runtime):
  cluster = rados.Rados(conffile='ceph.conf')
  cluster.connect()
  pool_name = 'test'
  ioctx = cluster.open_ioctx(pool_name)

  for w in workloads:
    w.start(ioctx)

  time.sleep(runtime);
  for w in workloads:
    w.stop()

  ioctx.close()

def dry_run(workloads, runtime):
  for w in workloads:
    w.dry_start()
  time.sleep(runtime/10);
  for w in workloads:
    w.stop()

workloads = list()

def main():
  parser = argparse.ArgumentParser(
      prog='Variable OSD load generator')
  parser.add_argument('--debug_level', type=str, default='1')
  parser.add_argument('--dry-run', action='store_true', default=False)
  parser.add_argument('--runtime', required=False, type=int, default=60)
  parser.add_argument('--workload', action='append', required=False, type=str, #nargs='*', 
                      help='Space separated list workload specs. '
                      'Default: "cnt=3 amp=500-1000 period=90-120 shift=0-1 supp=1"')
  args = parser.parse_args()

  log_levels = {
      '1': logging.CRITICAL,
      '2': logging.ERROR,
      '3': logging.WARNING,
      '4': logging.INFO,
      '5': logging.DEBUG,
      '6': logging.NOTSET
  }

  logging.basicConfig(level=log_levels[args.debug_level])
  logger = logging.getLogger("iots")
  logger.setLevel(log_levels[args.debug_level])

  wcnt = 0
  for i in args.workload:
    wparams = i.split(" ")
    wdict = dict()
    for j in wparams:
      if j.find('=') != -1:
        k = j.split('=')
        wdict[k[0]] = k[1]
    wsine = workload_sine(wdict);
    w = workload("workload_%d" % wcnt, wsine)
    wcnt = wcnt + 1
    workloads.append(w)

  if args.dry_run:
    dry_run(workloads, args.runtime)
  else:
    run(workloads, args.runtime)


if __name__ == '__main__':
  main()
