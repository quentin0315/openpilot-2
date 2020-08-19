from cereal import car
from common.numpy_fast import clip
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfa_mfa, \
                                             create_scc12, create_mdps12
from selfdrive.car.hyundai.values import Buttons, SteerLimitParams, CAR
from opendbc.can.packer import CANPacker
from selfdrive.config import Conversions as CV
from selfdrive.car.hyundai.spdcontroller  import SpdController

import common.log as trace1

VisualAlert = car.CarControl.HUDControl.VisualAlert

# Accel limits
ACCEL_HYST_GAP = 0.02  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 1.5  # 1.5 m/s2
ACCEL_MIN = -3.0 # 3   m/s2
ACCEL_SCALE = max(ACCEL_MAX, -ACCEL_MIN)

def accel_hysteresis(accel, accel_steady):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if accel > accel_steady + ACCEL_HYST_GAP:
    accel_steady = accel - ACCEL_HYST_GAP
  elif accel < accel_steady - ACCEL_HYST_GAP:
    accel_steady = accel + ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady

def process_hud_alert(enabled, fingerprint, visual_alert, left_lane,
                      right_lane, left_lane_depart, right_lane_depart, button_on):
  sys_warning = (visual_alert == VisualAlert.steerRequired)

  # initialize to no line visible
  sys_state = 1
  if not button_on:
    lane_visible = 0
  if left_lane and right_lane or sys_warning:  #HUD alert only display when LKAS status is active
    if enabled or sys_warning:
      sys_state = 3
    else:
      sys_state = 4
  elif left_lane:
    sys_state = 5
  elif right_lane:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if left_lane_depart:
    left_lane_warning = 1 if fingerprint in [CAR.HYUNDAI_GENESIS, CAR.GENESIS_G90, CAR.GENESIS_G80] else 2
  if right_lane_depart:
    right_lane_warning = 1 if fingerprint in [CAR.HYUNDAI_GENESIS, CAR.GENESIS_G90, CAR.GENESIS_G80] else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.car_fingerprint = CP.carFingerprint
    self.packer = CANPacker(dbc_name)
    self.accel_steady = 0
    self.apply_steer_last = 0
    self.steer_rate_limited = False
    self.lkas11_cnt = 0
    self.scc12_cnt = 0
    self.resume_cnt = 0
    self.last_resume_frame = 0
    self.last_lead_distance = 0
    self.turning_signal_timer = 0
    self.lkas_button_on = True
    self.longcontrol = False #TODO: make auto
    
    self.SC = SpdController()
    self.sc_wait_timer2 = 0
    self.sc_active_timer2 = 0     
    self.sc_btn_type = Buttons.NONE
    self.sc_clu_speed = 0
    self.speed_control_enabled = 1


  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, visual_alert,
             left_lane, right_lane, left_lane_depart, right_lane_depart, sm, LaC):

    path_plan = sm['pathPlan']
    # *** compute control surfaces ***

    # gas and brake
    apply_accel = actuators.gas - actuators.brake

    apply_accel, self.accel_steady = accel_hysteresis(apply_accel, self.accel_steady)
    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)

    # Steering Torque
    new_steer = actuators.steer * SteerLimitParams.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, SteerLimitParams)
    self.steer_rate_limited = new_steer != apply_steer

    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    # temporarily disable steering when LKAS button off 
    # lkas_active = enabled and abs(CS.out.steeringAngle) < 90. and self.lkas_button_on
    lkas_active = enabled and self.lkas_button_on

    # fix for Genesis hard fault at low speed
    if CS.out.vEgo < 60 * CV.KPH_TO_MS and self.car_fingerprint == CAR.HYUNDAI_GENESIS and not CS.mdps_bus:
      lkas_active = 0

    # Disable steering while turning blinker on and speed below 60 kph
    if CS.out.leftBlinker or CS.out.rightBlinker:
      self.turning_signal_timer = 100  # Disable for 1.0 Seconds after blinker turned off
    elif CS.left_blinker_flash or CS.right_blinker_flash: # Optima has blinker flash signal only
      self.turning_signal_timer = 100

    if self.turning_signal_timer and CS.out.vEgo < 60 * CV.KPH_TO_MS:
      lkas_active = 0
    if self.turning_signal_timer:
      self.turning_signal_timer -= 1
    if not lkas_active:
      apply_steer = 0

    self.apply_accel_last = apply_accel
    self.apply_steer_last = apply_steer

    sys_warning, sys_state, left_lane_warning, right_lane_warning =\
      process_hud_alert(lkas_active, self.car_fingerprint, visual_alert,
                        left_lane, right_lane, left_lane_depart, right_lane_depart,
                        self.lkas_button_on)

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 38 if CS.is_set_speed_in_mph  else 60
    if clu11_speed > enabled_speed or not lkas_active:
      enabled_speed = clu11_speed

    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"]
      self.scc12_cnt = CS.scc12["CR_VSM_Alive"] + 1 if not CS.no_radar else 0

    self.lkas11_cnt = (self.lkas11_cnt + 1) % 0x10
    self.scc12_cnt %= 0xF
    self.clu11_cnt = frame % 0x10

    can_sends = []
    can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 0))

    if CS.mdps_bus or CS.scc_bus == 1: # send lkas11 bus 1 if mdps or scc is on bus 1
      can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 1))
    if CS.mdps_bus: # send clu11 to mdps if it is not on bus 0
      can_sends.append(create_clu11(self.packer, self.clu11_cnt, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed))
      #can_sends.append(create_clu11(self.packer, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed, self.clu11_cnt))

    if pcm_cancel_cmd and self.longcontrol:
      can_sends.append(create_clu11(self.packer, self.clu11_cnt, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed))
      #can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed, self.clu11_cnt))
    elif CS.mdps_bus: # send mdps12 to LKAS to prevent LKAS error if no cancel cmd
      can_sends.append(create_mdps12(self.packer, frame, CS.mdps12))

    if CS.scc_bus and self.longcontrol and frame % 2: # send scc12 to car if SCC not on bus 0 and longcontrol enabled
      can_sends.append(create_scc12(self.packer, apply_accel, enabled, self.scc12_cnt, CS.scc12))
      self.scc12_cnt += 1

    if CS.out.cruiseState.standstill:
      # run only first time when the car stopped
      if self.last_lead_distance == 0:
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance != self.last_lead_distance and (frame - self.last_resume_frame) > 5:
        #can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed))
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed))
        #can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed, self.resume_cnt))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = frame
          #self.clu11_cnt = 0
          self.resume_cnt = 0
    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0  

    elif CS.out.driverOverride == 2 or not CS.out.cruiseState.enabled or CS.out.cruiseState.cluCruiseSwState in [1, 2]:
      #self.model_speed = 300
      self.resume_cnt = 0
      self.sc_btn_type = Buttons.NONE
      self.sc_wait_timer2 = 10
      self.sc_active_timer2 = 0
    elif self.sc_wait_timer2:
      self.sc_wait_timer2 -= 1

    #stock 모드가 아닐 경우에만 반영
    elif self.speed_control_enabled and CS.out.cruiseState.modeSel != 0:
      #acc_mode, clu_speed = self.long_speed_cntrl( v_ego_kph, CS, actuators )
      v_curvature = sm['plan'].pCurvature
      
      #btn_type, clu_speed = self.SC.update2(v_ego_kph, CS, sm, actuators, dRel, yRel, vRel, LaC.v_curvature )   # speed controller spdcontroller.py
      #btn_type, clu_speed = self.SC.update2(CS, sm) #, LaC.v_curvature )   # speed controller spdcontroller.py
      btn_type, clu_speed = self.SC.update2(CS, sm, v_curvature )   # speed controller spdcontroller.py
      #btn_type = 0 
      #clu_speed = 0
      

      if CS.out.vEgoKph < 5: #5km/h:
        self.sc_btn_type = Buttons.NONE
      elif self.sc_btn_type != Buttons.NONE:
        pass
      elif btn_type != Buttons.NONE:
        self.resume_cnt = 0
        self.sc_active_timer2 = 0
        self.sc_btn_type = btn_type
        self.sc_clu_speed = clu_speed

      if self.sc_btn_type != Buttons.NONE:
        self.sc_active_timer2 += 1
        if self.sc_active_timer2 > 10:
          self.sc_wait_timer2 = 5
          self.resume_cnt = 0
          self.sc_active_timer2 = 0
          self.sc_btn_type = Buttons.NONE          
        else:
          # 0, 1, 2 모드에서는  Set 상태에서만 가감속 전달
          # clu_cnt 짝수일때만 전달
          if self.resume_cnt % 2 == 0:
            if CS.out.cruiseState.cruiseLampSet:
              can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed))
              #can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed))
              #can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, self.sc_btn_type, clu11_speed))
              #can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed, ))
            # Set이 아니면서 3 모드이면 가감속 신호 전달
            elif CS.out.cruiseState.modeSel ==3 and CS.out.vEgoKph > 30:
              can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed))
              #can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed))
              #can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, self.sc_btn_type, clu11_speed))
              #can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed, self.resume_cnt))
            self.resume_cnt += 1
    
            str1 = 'btn_type={:03.0f} sc_clu_speed={:03.0f} resumeCnt={:03.0f} cluCnt={:03.0f} frame={:03.0f} '.format(
            self.sc_btn_type, self.sc_clu_speed, self.resume_cnt, self.clu11_cnt, frame )
            #str2 = ' curv={:0.3f}'.format(  v_curvature )

            str3 = str1
            print("spdBtnCtrl2:" + str3)
            trace1.printf( str3 )
    


    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.car_fingerprint in [CAR.SONATA, CAR.PALISADE, CAR.SONATA_H, CAR.SANTA_FE]:
      can_sends.append(create_lfa_mfa(self.packer, frame, enabled))

    return can_sends
