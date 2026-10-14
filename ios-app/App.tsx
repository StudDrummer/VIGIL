/**
 * VIGIL — Passive Health Monitor
 * App.tsx — Production Build v7.1 (Design polish pass)
 *
 * New in v7 (on top of v6):
 * - Multi-person detection UI: identify each person VIGIL sees,
 *   mark one as the resident, others as visitors
 * - Per-person trend screen (PersonDetailScreen) with its own
 *   gait speed / cadence / posture / asymmetry history
 * - Baseline-building progress indicator per identified person
 *
 * Carried over from v6:
 * - Full onboarding flow with AsyncStorage persistence
 * - Activity ring restored with SVG
 * - Camera stream smoothed (no flicker, crossfade)
 * - Fullscreen camera modal
 * - App loads saved profile on every launch
 * - Production-ready: works for any first-time user
 */

import React, {useCallback, useEffect, useRef, useState} from 'react';
import { Share, Linking,
  ActivityIndicator,
  Alert,
  Animated,
  AppState,
  AppStateStatus,
  Dimensions,
  Image,
  Keyboard,
  KeyboardAvoidingView,
  Modal,
  PanResponder,
  Platform,
  RefreshControl,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import Svg, {Circle, Line, Path} from 'react-native-svg';

let AppleHealthKit: any = null;
let HK_LOAD_ERROR: string | null = null;
try {
  const _hkmod = require('react-native-health');
  AppleHealthKit = _hkmod?.default ?? _hkmod; // handles both CJS and ESM module shapes
  if (!AppleHealthKit) HK_LOAD_ERROR = 'module resolved but exported nothing';
} catch (e: any) {
  HK_LOAD_ERROR = e?.message ?? String(e);
}

import axios from 'axios';

// ─────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────
const DEFAULT_IP    = 'your-jetson-ip';
const DEFAULT_PORT  = '5001';
const TIMEOUT_MS    = 10_000;
const AUTO_SYNC_SEC = 60;
const VIGIL_KEY     = 'set-your-api-key-here';
const FALLBACK_URL  = `http://${DEFAULT_IP}:${DEFAULT_PORT}`;

const SK_ONBOARDING = '@vigil_onboarding_v1';
const SK_PROFILE    = '@vigil_profile_v1';
const SK_SERVER     = '@vigil_server_v1';

const AX = axios.create({
  timeout: TIMEOUT_MS,
  headers: {'X-VIGIL-Key': VIGIL_KEY, 'Content-Type': 'application/json'},
});

const SW     = Dimensions.get('window').width;
const CAM_W  = SW - 32;
const CAM_H  = Math.round(CAM_W * 9 / 16);
const RING   = SW * 0.52;
const STROKE = 14;
const RADIUS = (RING - STROKE) / 2;
const CIRC   = 2 * Math.PI * RADIUS;

// LIVE-badge pulse — module scope so the loop needs no component hooks/refs
const LIVE_PULSE = new Animated.Value(1);
Animated.loop(
  Animated.sequence([
    Animated.timing(LIVE_PULSE,{toValue:1.15,duration:600,useNativeDriver:true}),
    Animated.timing(LIVE_PULSE,{toValue:1,duration:600,useNativeDriver:true}),
  ]),
).start();

// ─────────────────────────────────────────────
// COLORS
// ─────────────────────────────────────────────
const C = {
  bg:'#050807', card:'#0F1613', cardHi:'#141D19', fill:'#1A2420',
  border:'#24312B', hair:'rgba(255,255,255,0.07)',
  text:'#F2F6F3', sub:'#A3B0A9', muted:'#6C7A73',
  green:'#32D74B', red:'#FF453A', orange:'#FF9F0A',
  blue:'#0A84FF', purple:'#BF5AF2', teal:'#3DD6E3',
};

// ─────────────────────────────────────────────
// TYPES
// ─────────────────────────────────────────────
interface Sample {
  type:string; value:number; unit:string;
  startDate:string; endDate:string; is_daily_total?:boolean;
}
interface DailySummary {
  stride_variability?: number|null;
  date:string; step_count:number|null; hrv_sdnn:number|null;
  resting_hr:number|null; sleep_hours:number|null; active_calories:number|null;
  spo2_avg:number|null; walking_asymmetry_pct:number|null;
  walking_speed_ms:number|null; camera_gait_speed_ms:number|null;
  camera_cadence_spm:number|null; camera_asymmetry_pct:number|null;
  posture_score:number|null; active_minutes:number|null;
  still_minutes:number|null; activity_level_pct:number|null;
  longest_stillness_min:number|null; camera_data_points:number|null;
  anomaly_flag:number;
}
interface CameraStatus {
  online:boolean; fps:number|null; person_present:boolean;
  person_count?:number; last_seen_minutes_ago:number|null; camera_mode:string|null;
}
interface Room {
  id:string; name:string; resident_name:string|null;
  emergency_phone:string|null; camera_online:boolean;
  person_present:boolean; person_count:number; fps:number|null;
}
interface AlertEntry {
  id:number; alert_type:string; detected_at:string;
  payload:any; seen:number; created_at:string;
}
interface MLScore {
  condition_id:string; condition_label:string; score_0_100:number;
  probability:number; published_auc:number; data_quality:number;
  days_of_data:number; insufficient_data:number;
}
interface DebugEntry { ts:string; level:'INFO'|'WARN'|'ERROR'|'OK'; msg:string; }

interface Person {
  id:string; name:string|null; room_id:string; is_resident:number;
  height_px:number|null; confidence:number; frames_seen:number;
  first_seen_at:string|null; last_seen_at:string|null;
}
interface PersonAlert {
  id:number; alert_type:string; room_id:string;
  person_ids:string[]; resolved:number; created_at:string;
}
interface PersonTrendDay {
  date:string; avg_speed:number|null; avg_cadence:number|null;
  avg_posture:number|null; avg_asymmetry:number|null; samples:number;
}
interface PersonBaselineStatus {
  person_id:string; has_baseline:boolean;
  days_collected:number; days_needed:number;
  baseline:{avg_speed?:{mean:number;std:number};avg_cadence?:{mean:number;std:number};
    avg_posture?:{mean:number;std:number};avg_asymmetry?:{mean:number;std:number};}|null;
}

interface SavedProfile {
  residentName:string; residentHeight:string;
  emergencyPhone:string; emergencyEmail:string; watchEnabled:boolean;
}

type MainTab    = 'today'|'trends'|'alerts'|'insights'|'settings';
type SettingTab = 'resident'|'server'|'camera'|'support';

// ─────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────
const fmt = (v:number|null|undefined, d=1):string => {
  if (v==null||isNaN(Number(v))) return '—';
  return Number(v).toFixed(d);
};
const fmtLarge = (v:number|null|undefined):string => {
  if (v==null||isNaN(Number(v))) return '—';
  const n=Number(v);
  return n>=1000?(n/1000).toFixed(1)+'k':Math.round(n).toString();
};
const timeAgo = (iso:string):string => {
  try {
    const m=Math.floor((Date.now()-new Date(iso).getTime())/60000);
    if(m<1)return'just now'; if(m<60)return m+'m ago';
    const h=Math.floor(m/60); return h<24?h+'h ago':Math.floor(h/24)+'d ago';
  } catch { return iso; }
};
const todayRange = () => {
  const now=new Date(); const s=new Date(now); s.setHours(0,0,0,0);
  return {startDate:s.toISOString(),endDate:now.toISOString(),ascending:false,limit:1000};
};
const rollingRange = (hours=168) => {
  const end=new Date(); const start=new Date(end.getTime()-hours*3600000);
  return {startDate:start.toISOString(),endDate:end.toISOString(),ascending:false,limit:500};
};

function readSamples(method:string,opts:object,type:string,unit:string,
  log:(l:DebugEntry['level'],m:string)=>void):Promise<Sample[]> {
  return new Promise(res=>{
    if(!AppleHealthKit)return res([]);
    const fn=(AppleHealthKit as any)[method];
    if(typeof fn!=='function'){return res([]);}
    try{
      fn.call(AppleHealthKit,opts,(err:any,raw:any)=>{
        if(err||!raw)return res([]);
        const arr=Array.isArray(raw)?raw:[raw];
        res(arr.filter((s:any)=>s?.value!=null&&!isNaN(Number(s.value)))
          .map((s:any):Sample=>({
            type,unit,value:Number(s.value),
            startDate:s.startDate??new Date().toISOString(),
            endDate:s.endDate??s.startDate??new Date().toISOString(),
          })));
      });
    }catch(e:any){log('ERROR',`${method}: ${e?.message}`);res([]);}
  });
}

function readDailyTotal(method:string,opts:object,type:string,unit:string,
  log:(l:DebugEntry['level'],m:string)=>void):Promise<Sample[]> {
  return new Promise(res=>{
    if(!AppleHealthKit)return res([]);
    const fn=(AppleHealthKit as any)[method];
    if(typeof fn!=='function')return res([]);
    try{
      fn.call(AppleHealthKit,opts,(err:any,raw:any)=>{
        if(err||raw==null)return res([]);
        const arr=Array.isArray(raw)?raw:[raw];
        const total=arr.reduce((s:number,x:any)=>s+Number(x?.value??x??0),0);
        if(!total||isNaN(total))return res([]);
        const m=new Date();m.setHours(0,0,0,0);
        res([{type,unit,value:total,startDate:m.toISOString(),endDate:new Date().toISOString(),is_daily_total:true}]);
      });
    }catch{res([]);}
  });
}

// ─────────────────────────────────────────────
// ONBOARDING
// ─────────────────────────────────────────────
interface OBProps { onComplete:(profile:SavedProfile,serverIp:string,serverPort:string)=>void; onDemo?:()=>void; }

function OnboardingScreen({onComplete,onDemo}:OBProps) {
  const [step,setStep]             = useState(0);
  const [residentName,setRN]       = useState('');
  const [dob,setDOB]               = useState('');
  const [residentHeight,setRH]     = useState('170');
  const [emergencyPhone,setEP]     = useState('');
  const [emergencyEmail,setEE]     = useState('');
  const [watchEnabled,setWE]       = useState(false);
  const [serverIp,setSI]           = useState(DEFAULT_IP);
  const [serverPort,setSP]         = useState(DEFAULT_PORT);
  const [testing,setTesting]       = useState(false);
  const [serverOk,setServerOk]     = useState<boolean|null>(null);
  const [agreed,setAgreed]         = useState(false);
  const TOTAL = 6;

  const testConnection = async () => {
    setTesting(true); setServerOk(null);
    try {
      const r = await axios.get(`http://${serverIp}:${serverPort}/health`,{timeout:6000});
      setServerOk(r.data?.status==='ok');
    } catch { setServerOk(false); }
    setTesting(false);
  };

  const canNext = () => {
    if(step===0)return agreed;
    if(step===1)return residentName.trim().length>0 && residentHeight.trim().length>0;
    if(step===2)return emergencyPhone.trim().length>=10;
    return true;
  };

  const OBField = ({label,value,onChange,placeholder,keyboard='default',note}:any) => (
    <View style={{marginBottom:20}}>
      <Text style={{color:C.sub,fontSize:12,fontWeight:'600',letterSpacing:0.5,textTransform:'uppercase',marginBottom:8}}>{label}</Text>
      {note && <Text style={{color:C.muted,fontSize:12,marginBottom:8,lineHeight:17}}>{note}</Text>}
      <View style={{backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.border}}>
        <TextInput
          style={{color:C.text,fontSize:15,paddingHorizontal:16,paddingVertical:14}}
          value={value} onChangeText={onChange} placeholder={placeholder}
          placeholderTextColor={C.muted} keyboardType={keyboard}
          autoCapitalize="none" autoCorrect={false}/>
      </View>
    </View>
  );

  const renderStep = () => {
    switch(step) {
      case 0: return (
        <View style={{flex:1}}>
          <View style={{alignItems:'center',marginTop:20,marginBottom:32}}>
            <View style={{width:80,height:80,borderRadius:20,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center',marginBottom:16}}>
              <Text style={{fontSize:36}}>◎</Text>
            </View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.5,marginBottom:8}}>Welcome to VIGIL</Text>
            <Text style={{color:C.sub,fontSize:15,textAlign:'center',lineHeight:22,paddingHorizontal:8}}>
              Passive health monitoring for retirement homes. VIGIL watches for changes in activity, gait, and vital signs — alerting caregivers when something seems off.
            </Text>
          </View>
          <View style={{backgroundColor:C.card,borderRadius:16,padding:20,marginBottom:20}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:12}}>Before you begin</Text>
            {[
              ['Camera required','A Tapo or Reolink camera connected to the same Wi-Fi as the VIGIL server.'],
              ['VIGIL monitoring unit','The small computer that analyzes the camera feed inside the room.'],
              ['Optional: Apple Watch','If the resident has an Apple Watch, VIGIL can sync health metrics automatically.'],
            ].map(([title,desc])=>(
              <View key={title} style={{flexDirection:'row',gap:12,marginBottom:12}}>
                <View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,marginTop:6}}/>
                <View style={{flex:1}}>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}}>{title}</Text>
                  <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginTop:2}}>{desc}</Text>
                </View>
              </View>
            ))}
          </View>
          <TouchableOpacity style={{flexDirection:'row',alignItems:'flex-start',gap:12,marginBottom:20}} onPress={()=>setAgreed(p=>!p)}>
            <View style={{width:22,height:22,borderRadius:6,borderWidth:2,borderColor:agreed?C.green:C.muted,backgroundColor:agreed?C.green:'transparent',alignItems:'center',justifyContent:'center',marginTop:1}}>
              {agreed && <Text style={{color:'#000',fontSize:13,fontWeight:'800'}}>✓</Text>}
            </View>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,flex:1}}>
              I understand VIGIL is a wellness monitoring tool and not a medical device. I have read and agree to the Terms of Service and Privacy Policy.
            </Text>
          </TouchableOpacity>
        </View>
      );
      case 1: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Resident Profile</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            This information personalises monitoring and is used to calibrate gait detection for this specific resident.
          </Text>
          <OBField label="Full Name" value={residentName} onChange={setRN} placeholder="e.g. Margaret Johnson"
            note="Used in alert messages sent to emergency contacts."/>
          <OBField label="Date of Birth" value={dob} onChange={setDOB} placeholder="MM/DD/YYYY"/>
          <OBField label="Height (cm)" value={residentHeight} onChange={setRH} placeholder="170" keyboard="number-pad"
            note="Used to calibrate walking speed measurements from the camera."/>
        </View>
      );
      case 2: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Emergency Contact</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            This person receives an SMS immediately when a fall or extended period of inactivity is detected.
          </Text>
          <View style={{backgroundColor:C.card,borderRadius:12,borderWidth:1,borderColor:C.border,padding:14,marginBottom:24}}>
            <Text style={{color:C.text,fontSize:13,fontWeight:'600',marginBottom:4}}>Text message alerts</Text>
            <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
              Enter the mobile number that should receive fall and inactivity alerts for this resident. Message and data rates may apply. Reply STOP at any time to stop receiving messages, or HELP for help. This number is never shared for marketing.
            </Text>
            <View style={{flexDirection:'row',gap:16,marginTop:10}}>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'600'}}
                onPress={()=>Linking.openURL('https://vigilhealth.me/privacy.html')}>Privacy Policy</Text>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'600'}}
                onPress={()=>Linking.openURL('https://vigilhealth.me/terms.html')}>Terms</Text>
            </View>
          </View>
          <OBField label="Phone Number" value={emergencyPhone} onChange={setEP} placeholder="+1 (206) 555-0000" keyboard="phone-pad"
            note="Must include country code. e.g. +12065550000"/>
          <OBField label="Email (optional)" value={emergencyEmail} onChange={setEE} placeholder="family@example.com" keyboard="email-address"/>
        </View>
      );
      case 3: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Connect Your Monitoring Unit</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            Enter the address of your VIGIL monitoring unit. It is printed on the setup card that comes with the unit, or your facility administrator can provide it. Your phone needs to be on the same network as the unit.
          </Text>
          <OBField label="Unit Address" value={serverIp} onChange={setSI} placeholder="e.g. 192.168.1.50"
            keyboard="numbers-and-punctuation"
            note="Shown on the setup card that came with your unit."/>
          <OBField label="Port" value={serverPort} onChange={setSP} placeholder="5001" keyboard="number-pad"/>
          {!!onDemo && (
            <TouchableOpacity onPress={onDemo} style={{alignItems:'center',paddingVertical:8,marginBottom:12}}>
              <Text style={{color:C.blue,fontSize:13,fontWeight:'600'}}>No unit yet? Explore VIGIL in Demo Mode</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity
            style={{backgroundColor:testing?C.fill:C.blue,borderRadius:12,padding:14,alignItems:'center',marginBottom:16,opacity:testing?0.7:1}}
            onPress={testConnection} disabled={testing}>
            {testing
              ? <View style={{flexDirection:'row',gap:8,alignItems:'center'}}><ActivityIndicator color="#fff" size="small"/><Text style={{color:'#fff',fontWeight:'700'}}>Testing...</Text></View>
              : <Text style={{color:'#fff',fontSize:15,fontWeight:'700'}}>Test Connection</Text>}
          </TouchableOpacity>
          {serverOk===true && (
            <View style={{backgroundColor:C.green+'18',borderRadius:12,padding:12,alignItems:'center'}}>
              <Text style={{color:C.green,fontWeight:'700'}}>✓  Connected successfully</Text>
            </View>
          )}
          {serverOk===false && (
            <View style={{backgroundColor:C.red+'18',borderRadius:12,padding:12}}>
              <Text style={{color:C.red,fontWeight:'700',marginBottom:4}}>✕  Connection failed</Text>
              <Text style={{color:C.red,fontSize:12,lineHeight:18}}>
                Check that the monitoring unit is powered on and that your phone is on the same network as the unit, then try again.
              </Text>
            </View>
          )}
          {serverOk===false && !!onDemo && (
            <View style={{marginTop:14,backgroundColor:C.card,borderRadius:12,
                          padding:14,borderWidth:1,borderColor:C.border}}>
              <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginBottom:12}}>
                No monitoring unit available? Demo Mode shows every feature
                using sample data, with no hardware required.
              </Text>
              <TouchableOpacity onPress={onDemo}
                style={{backgroundColor:C.green,borderRadius:12,paddingVertical:12,
                        alignItems:'center'}}>
                <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Open Demo Mode</Text>
              </TouchableOpacity>
            </View>
          )}
        </View>
      );
      case 4: return (
        <View style={{flex:1}}>
          <Text style={{color:C.text,fontSize:24,fontWeight:'800',marginBottom:6}}>Apple Watch</Text>
          <Text style={{color:C.sub,fontSize:14,marginBottom:28,lineHeight:20}}>
            VIGIL works fully with the camera alone. Connecting an Apple Watch adds heart rate, HRV, sleep, and SpO2 data as additional health signals.
          </Text>
          <TouchableOpacity
            style={{backgroundColor:C.card,borderRadius:16,padding:20,marginBottom:12,borderWidth:2,borderColor:watchEnabled?C.green:C.border}}
            onPress={()=>setWE(true)}>
            <View style={{flexDirection:'row',alignItems:'center',gap:14}}>
              <View style={{width:44,height:44,borderRadius:12,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center'}}>
                <Text style={{fontSize:22}}>⌚</Text>
              </View>
              <View style={{flex:1}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Yes — connect Apple Watch</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:3}}>Adds HRV, heart rate, sleep, and SpO2 data</Text>
              </View>
              {watchEnabled && <View style={{width:20,height:20,borderRadius:10,backgroundColor:C.green,alignItems:'center',justifyContent:'center'}}><Text style={{color:'#000',fontSize:12,fontWeight:'800'}}>✓</Text></View>}
            </View>
          </TouchableOpacity>
          <TouchableOpacity
            style={{backgroundColor:C.card,borderRadius:16,padding:20,borderWidth:2,borderColor:!watchEnabled?C.green:C.border}}
            onPress={()=>setWE(false)}>
            <View style={{flexDirection:'row',alignItems:'center',gap:14}}>
              <View style={{width:44,height:44,borderRadius:12,backgroundColor:C.fill,alignItems:'center',justifyContent:'center'}}>
                <Text style={{fontSize:22}}>◎</Text>
              </View>
              <View style={{flex:1}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>No — camera only</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:3}}>VIGIL monitors activity, gait, and detects falls automatically</Text>
              </View>
              {!watchEnabled && <View style={{width:20,height:20,borderRadius:10,backgroundColor:C.green,alignItems:'center',justifyContent:'center'}}><Text style={{color:'#000',fontSize:12,fontWeight:'800'}}>✓</Text></View>}
            </View>
          </TouchableOpacity>
        </View>
      );
      case 5: return (
        <View style={{flex:1}}>
          <View style={{alignItems:'center',marginTop:20,marginBottom:32}}>
            <View style={{width:80,height:80,borderRadius:40,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center',marginBottom:16}}>
              <Text style={{fontSize:36}}>◎</Text>
            </View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.5,marginBottom:8}}>All Set</Text>
            <Text style={{color:C.sub,fontSize:15,textAlign:'center',lineHeight:22}}>
              VIGIL is ready to monitor {residentName||'the resident'}. The camera will begin learning their normal activity pattern immediately.
            </Text>
          </View>
          {[
            ['Resident',residentName||'—'],
            ['Height',`${residentHeight} cm`],
            ['Emergency Contact',emergencyPhone||'—'],
            ['Server',`${serverIp}:${serverPort}`],
            ['Apple Watch',watchEnabled?'Connected':'Camera only'],
          ].map(([label,val])=>(
            <View key={label} style={{flexDirection:'row',justifyContent:'space-between',paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.border}}>
              <Text style={{color:C.sub,fontSize:14}}>{label}</Text>
              <Text style={{color:C.text,fontSize:14,fontWeight:'500'}}>{val}</Text>
            </View>
          ))}
          <View style={{marginTop:24,backgroundColor:C.card,borderRadius:14,padding:16}}>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,textAlign:'center'}}>
              You can update any of these settings at any time from the Settings tab. VIGIL learns the resident's baseline over the first 3 days of monitoring.
            </Text>
          </View>
        </View>
      );
      default: return null;
    }
  };

  const stepTitles = ['Welcome','Resident','Emergency','Server','Watch','Ready'];

  const finish = async () => {
    const profile:SavedProfile = {residentName,residentHeight,emergencyPhone,emergencyEmail,watchEnabled};
    await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
    await AsyncStorage.setItem(SK_SERVER,JSON.stringify({ip:serverIp,port:serverPort}));
    await AsyncStorage.setItem(SK_ONBOARDING,'done');
    onComplete(profile,serverIp,serverPort);
  };

  return (
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content"/>
      {/* Progress */}
      <View style={{paddingHorizontal:20,paddingTop:12,paddingBottom:4}}>
        <View style={{flexDirection:'row',gap:4,marginBottom:6}}>
          {Array.from({length:TOTAL}).map((_,i)=>(
            <View key={i} style={{flex:1,height:3,borderRadius:2,backgroundColor:i<=step?C.green:C.fill}}/>
          ))}
        </View>
        <Text style={{color:C.muted,fontSize:11,textAlign:'right'}}>{stepTitles[step]} · Step {step+1} of {TOTAL}</Text>
      </View>

      <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':undefined}>
        <ScrollView contentContainerStyle={{padding:24,paddingBottom:12}} keyboardShouldPersistTaps="handled" showsVerticalScrollIndicator={false}>
          {renderStep()}
        </ScrollView>
        <View style={{flexDirection:'row',gap:12,padding:20,paddingBottom:32}}>
          {step>0 && (
            <TouchableOpacity style={{flex:1,backgroundColor:C.fill,borderRadius:14,padding:16,alignItems:'center'}} onPress={()=>setStep(s=>s-1)}>
              <Text style={{color:C.text,fontWeight:'700',fontSize:15}}>Back</Text>
            </TouchableOpacity>
          )}
          <TouchableOpacity
            style={[s.btnPrimary,{flex:2},!canNext()&&{backgroundColor:C.fill,shadowOpacity:0}]}
            onPress={()=>step<TOTAL-1?setStep(s=>s+1):finish()}
            disabled={!canNext()}>
            <Text style={{color:canNext()?'#000':C.muted,fontWeight:'700',fontSize:15}}>
              {step===TOTAL-1?'Start Monitoring':'Continue'}
            </Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────
// ACTIVITY RING
// ─────────────────────────────────────────────
function ActivityRing({pct,activeMin,stillMin}:{pct:number|null;activeMin:number|null;stillMin:number|null}) {
  const fraction = Math.min(1,Math.max(0,(pct??0)/100));
  const dash = fraction * CIRC;
  const cx = RING/2;
  return (
    <View style={{alignItems:'center',paddingVertical:20}}>
      <View style={{width:RING,height:RING,justifyContent:'center',alignItems:'center'}}>
        <Svg width={RING} height={RING} style={{position:'absolute'}}>
          <Circle cx={cx} cy={cx} r={RADIUS} stroke={C.green+'28'} strokeWidth={STROKE} fill="none"/>
          {fraction>0 && (
            <Circle cx={cx} cy={cx} r={RADIUS} stroke={C.green} strokeWidth={STROKE} fill="none"
              strokeDasharray={`${dash} ${CIRC-dash}`} strokeLinecap="round"
              rotation={-90} origin={`${cx},${cx}`}/>
          )}
        </Svg>
        <View style={{alignItems:'center',backgroundColor:'rgba(0,0,0,0.85)',paddingHorizontal:18,paddingVertical:12,borderRadius:28}}>
          <Text style={{color:C.muted,fontSize:11,fontWeight:'700',letterSpacing:1.5,textTransform:'uppercase'}}>Activity</Text>
          <Text style={{color:C.green,fontSize:42,fontWeight:'800',lineHeight:48,marginTop:2}}>
            {pct!=null?Math.round(pct)+'%':'—'}
          </Text>
          <Text style={{color:C.muted,fontSize:11,marginTop:1}}>of in-view time moving</Text>
        </View>
      </View>
      <View style={{flexDirection:'row',gap:32,marginTop:16}}>
        <View style={{alignItems:'center'}}>
          <View style={{flexDirection:'row',alignItems:'center',gap:6,marginBottom:4}}>
            <View style={{width:8,height:8,borderRadius:4,backgroundColor:C.green}}/>
            <Text style={{color:C.sub,fontSize:12}}>Active</Text>
          </View>
          <Text style={{color:C.text,fontSize:18,fontWeight:'800'}}>
            {activeMin!=null?Math.round(activeMin)+' min':'—'}
          </Text>
        </View>
        <View style={{width:1,backgroundColor:C.border}}/>
        <View style={{alignItems:'center'}}>
          <View style={{flexDirection:'row',alignItems:'center',gap:6,marginBottom:4}}>
            <View style={{width:8,height:8,borderRadius:4,backgroundColor:C.orange}}/>
            <Text style={{color:C.sub,fontSize:12}}>Still</Text>
          </View>
          <Text style={{color:C.text,fontSize:18,fontWeight:'800'}}>
            {stillMin!=null?Math.round(stillMin)+' min':'—'}
          </Text>
        </View>
      </View>
    </View>
  );
}

// ─────────────────────────────────────────────
// AI INSIGHTS SCREEN
// ─────────────────────────────────────────────
function PersonDetailScreen({
  personId, personName, serverUrl, onClose, demoMode,
}:{personId:string; personName:string; serverUrl:string; onClose:()=>void;
   demoMode?:boolean}) {
  const [trends,setTrends]         = useState<PersonTrendDay[]>([]);
  const [baseline,setBaseline]     = useState<PersonBaselineStatus|null>(null);
  const [weeklyTrend,setWeeklyTrend] = useState<any>(null);
  const [pdLoading,setPdLoading]   = useState(true);

  const load = useCallback(async()=>{
    if(demoMode){
      setTrends(DEMO_PTRENDS); setBaseline(DEMO_PBASELINE);
      setWeeklyTrend(DEMO_WEEKLY); setPdLoading(false);
      return;
    }
    setPdLoading(true);
    const u = serverUrl||FALLBACK_URL;
    try{
      const [t,b,w] = await Promise.all([
        AX.get(`${u}/persons/${personId}/detections?days=14`,{timeout:8000}),
        AX.get(`${u}/persons/${personId}/baseline-status`,{timeout:8000}),
        AX.get(`${u}/persons/${personId}/weekly-trend`,{timeout:8000}).catch(()=>({data:null})),
      ]);
      if(Array.isArray(t.data))setTrends(t.data);
      if(b.data)setBaseline(b.data);
      if(w.data)setWeeklyTrend(w.data);
    }catch(_){}
    finally{setPdLoading(false);}
  },[serverUrl,personId,demoMode]);

  useEffect(()=>{load();},[load]);

  const PTrendBar=({data,label,unit,color,dec=1}:{data:(number|null)[];label:string;unit:string;color:string;dec?:number})=>{
    const valid=data.filter(v=>v!=null) as number[];
    if(!valid.length)return null;
    const mean=valid.reduce((a,b)=>a+b,0)/valid.length;
    const min=Math.min(...valid); const max=Math.max(...valid); const range=max-min||1;
    const latest=valid[valid.length-1];
    const BAR=44;
    return(
      <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
        <View style={{flexDirection:'row',justifyContent:'space-between',marginBottom:8}}>
          <View>
            <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600',marginBottom:2}}>{label}</Text>
            <Text style={{color,fontSize:20,fontWeight:'800'}}>{latest!=null?latest.toFixed(dec):'—'}{' '}<Text style={{color:C.sub,fontSize:11}}>{unit}</Text></Text>
          </View>
          <Text style={{color:C.muted,fontSize:11}}>{personName}'s avg {mean.toFixed(dec)}</Text>
        </View>
        <View style={{height:BAR,flexDirection:'row',alignItems:'flex-end',gap:3}}>
          {data.map((v,i)=>{
            if(v==null)return <View key={i} style={{flex:1,height:3,backgroundColor:C.muted+'20',borderRadius:2}}/>;
            const h=Math.max(4,((v-min)/range)*BAR);
            return <View key={i} style={{flex:1,height:h,backgroundColor:color,borderRadius:3,opacity:i===data.length-1?1:0.6}}/>;
          })}
        </View>
      </View>
    );
  };

  const daysCollected = baseline?.days_collected ?? trends.length;
  const daysNeeded = baseline?.days_needed ?? 7;
  const hasBaseline = baseline?.has_baseline ?? (daysCollected>=daysNeeded);
  const progressPct = Math.min(100,(daysCollected/Math.max(1,daysNeeded))*100);

  return(
    <View style={{flex:1,backgroundColor:C.bg}}>
      <SafeAreaView style={{flex:1}}>
        <StatusBar barStyle="light-content"/>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:20,paddingTop:12,paddingBottom:8}}>
          <View>
            <Text style={{color:C.text,fontSize:28,fontWeight:'800'}}>{personName}</Text>
            <Text style={{color:C.sub,fontSize:13,marginTop:2}}>Individual trend history</Text>
          </View>
          <TouchableOpacity onPress={onClose} style={{backgroundColor:C.card,borderRadius:18,width:36,height:36,alignItems:'center',justifyContent:'center'}}>
            <Text style={{color:C.text,fontSize:16}}>Close</Text>
          </TouchableOpacity>
        </View>
        {pdLoading?(
          <View style={{flex:1,alignItems:'center',justifyContent:'center'}}><ActivityIndicator color={C.green} size="large"/></View>
        ):(
          <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
            <View style={{marginHorizontal:16,marginTop:8,marginBottom:8,backgroundColor:C.card,borderRadius:16,padding:16}}>
              <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>{hasBaseline?'Baseline Established':'Building Baseline'}</Text>
                <Text style={{color:hasBaseline?C.green:C.orange,fontSize:13,fontWeight:'600'}}>{hasBaseline?'Active':`Day ${daysCollected} of ${daysNeeded}`}</Text>
              </View>
              <View style={{height:6,backgroundColor:C.fill,borderRadius:3}}>
                <View style={{height:6,width:`${progressPct}%` as any,backgroundColor:C.green,borderRadius:3}}/>
              </View>
              <Text style={{color:C.sub,fontSize:12,marginTop:10,lineHeight:18}}>
                {hasBaseline
                  ? `VIGIL has learned ${personName}'s normal walking speed, posture, and cadence from ${daysCollected} days of data. Today's readings are compared against their personal baseline.`
                  : `VIGIL is observing ${personName}'s normal patterns. After ${daysNeeded} days of data, anomaly detection compares future readings against their own baseline.`}
              </Text>
            </View>
            {/* Week-over-week comparison card */}
            {weeklyTrend?.changes && (()=>{
              const c = weeklyTrend.changes;
              const items = [
                {label:'Walking Speed', val:c.gait_speed_pct},
                {label:'Cadence',       val:c.cadence_pct},
                {label:'Posture',       val:c.posture_pct},
              ].filter(x=>x.val!=null);
              if(!items.length) return null;
              return(
                <View style={{marginHorizontal:16,marginBottom:8,backgroundColor:C.card,borderRadius:16,padding:16}}>
                  <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:12}}>vs. Last Week</Text>
                  {items.map(item=>{
                    const up=item.val>0;
                    const neutral=Math.abs(item.val)<3;
                    const color=neutral?C.sub:up?C.green:C.red;
                    return(
                      <View key={item.label} style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
                        <Text style={{color:C.sub,fontSize:13}}>{item.label}</Text>
                        <Text style={{color,fontSize:14,fontWeight:'700'}}>{up?'+':''}{item.val.toFixed(1)}%</Text>
                      </View>
                    );
                  })}
                </View>
              );
            })()}

            {trends.length===0?(
              <View style={{alignItems:'center',paddingVertical:48}}>
                <Text style={{color:C.sub,fontSize:15,fontWeight:'500'}}>No trend data yet</Text>
                <Text style={{color:C.muted,fontSize:12,marginTop:4}}>Data appears after {personName} is seen walking by the camera</Text>
              </View>
            ):(
              <>
                <Text style={s.secHeader}>Gait & Posture — Last 14 Days</Text>
                <PTrendBar data={trends.map(t=>t.avg_speed)} label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
                <PTrendBar data={trends.map(t=>t.avg_cadence)} label="Cadence" unit="spm" color={C.blue} dec={0}/>
                <PTrendBar data={trends.map(t=>t.avg_posture)} label="Posture Score" unit="/100" color={C.purple} dec={0}/>
                <PTrendBar data={trends.map(t=>t.avg_asymmetry)} label="Gait Asymmetry" unit="%" color={C.orange} dec={1}/>
              </>
            )}
            <View style={{height:40}}/>
          </ScrollView>
        )}
      </SafeAreaView>
    </View>
  );
}

// ─────────────────────────────────────────────
// PERSON PHOTO CARD — shows captured snapshot in identification modal
// ─────────────────────────────────────────────
// FALL_REPLAY_PLAYER: stick-figure replay of the 30 s before a fall.
// Coordinates only; no video is ever stored.
type ReplayFrame = [number, number[][], number[]];
const RP_EDGES:[number,number][] = [[5,6],[5,7],[7,9],[6,8],[8,10],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16],[0,5],[0,6]];
function demoReplay():ReplayFrame[]{
  // Sample data for Demo Mode: standing, a fall to the side, lying still.
  const stand=[[.50,.20],[.49,.19],[.51,.19],[.48,.20],[.52,.20],[.46,.29],[.54,.29],[.45,.39],[.55,.39],[.45,.48],[.55,.48],[.47,.50],[.53,.50],[.47,.65],[.53,.65],[.47,.80],[.53,.80]];
  const lie=[[.30,.78],[.29,.77],[.29,.79],[.30,.76],[.30,.80],[.37,.76],[.37,.81],[.45,.75],[.45,.82],[.52,.75],[.52,.82],[.55,.77],[.55,.80],[.67,.77],[.67,.80],[.79,.78],[.79,.79]];
  const out:ReplayFrame[]=[];
  for(let i=0;i<100;i++){
    const t=i*0.2;
    let f=0;
    if(i>=40&&i<45)f=(i-40)/4; else if(i>=45)f=1;
    const sway=i<40?Math.sin(i/4)*0.004:0;
    const pts=stand.map((p,k)=>[p[0]+(lie[k][0]-p[0])*f+sway,p[1]+(lie[k][1]-p[1])*f]);
    out.push([t,pts,stand.map(()=>0.9)]);
  }
  return out;
}
function FallReplayPlayer({frames:allFrames}:{frames:ReplayFrame[]}){
  // Only the 30 s before the alert, in time order. Older replays were saved
  // with stale samples from long before the fall.
  const frames=React.useMemo(()=>{
    const f=[...allFrames].sort((a,b)=>a[0]-b[0]);
    if(!f.length)return f;
    const end=f[f.length-1][0];
    return f.filter(x=>end-x[0]<=30);
  },[allFrames]);
  const [i,setI]=useState(0);
  useEffect(()=>{
    if(!frames.length)return;
    const id=setInterval(()=>setI(v=>(v+1)%frames.length),200);
    return()=>clearInterval(id);
  },[frames]);
  const W=SW-48, H=Math.round(W*9/16);
  const f=frames[Math.min(i,frames.length-1)];
  if(!f)return null;
  const [pts,conf]=[f[1],f[2]];
  const ok=(k:number)=>(conf[k]??0)>=0.3;
  const secsBeforeEnd=Math.max(0,(frames[frames.length-1][0]-f[0])).toFixed(1);
  return(
    <View>
      <View style={{width:W,height:H,backgroundColor:'#181818',borderRadius:14,overflow:'hidden'}}>
        <Svg width={W} height={H}>
          {RP_EDGES.map(([a,b],n)=>ok(a)&&ok(b)?(
            <Line key={n} x1={pts[a][0]*W} y1={pts[a][1]*H} x2={pts[b][0]*W} y2={pts[b][1]*H}
              stroke={C.green} strokeWidth={4} strokeLinecap="round"/>
          ):null)}
          {ok(0)&&<Circle cx={pts[0][0]*W} cy={pts[0][1]*H} r={Math.max(6,W*0.018)} stroke={C.green} strokeWidth={3} fill="none"/>}
        </Svg>
      </View>
      <View style={{height:4,backgroundColor:C.fill,borderRadius:2,marginTop:10,overflow:'hidden'}}>
        <View style={{width:`${(i/Math.max(1,frames.length-1))*100}%`,height:4,backgroundColor:C.red}}/>
      </View>
      <Text style={{color:C.muted,fontSize:11,marginTop:6}}>{`${secsBeforeEnd} s before the alert · plays on a loop`}</Text>
    </View>
  );
}

function PersonPhotoCard({personId, serverUrl, large=false}:{personId:string; serverUrl:string; large?:boolean}) {
  // Each person has exactly one stored photo. Until the camera has taken it,
  // keep checking every few seconds instead of showing "?" forever.
  const [photo, setPhoto] = useState<string|null>(null);
  useEffect(()=>{
    let alive=true; let tries=0;
    const load=()=>{
      AX.get(`${serverUrl||FALLBACK_URL}/persons/${personId}/photo`,{timeout:5000})
        .then(r=>{
          if(!alive)return;
          if(r.data?.available&&r.data?.photo_b64){setPhoto(r.data.photo_b64);return;}
          if(++tries<40)setTimeout(load,3000);
        })
        .catch(()=>{if(alive&&++tries<40)setTimeout(load,3000);});
    };
    load();
    return()=>{alive=false;};
  },[personId,serverUrl]);
  const W=large?160:96, H=large?160:72;
  if(!photo) return (
    <View style={{width:W,height:H,borderRadius:12,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',marginBottom:10,alignSelf:large?'center':'flex-start'}}>
      <Text style={{color:C.muted,fontSize:large?13:24,textAlign:'center'}}>{large?'Taking photo...':'?'}</Text>
    </View>
  );
  return (
    <Image source={{uri:`data:image/jpeg;base64,${photo}`}}
      style={{width:W,height:H,borderRadius:12,marginBottom:10,alignSelf:large?'center':'flex-start'}}
      resizeMode="cover"/>
  );
}

// PEOPLE_UI_V1: photo avatars and a person picker, shared by every screen.
const PHOTO_CACHE:{[k:string]:string} = {};
function PersonAvatar({personId,name,serverUrl,size=44,isResident=false}:
  {personId:string; name:string|null|undefined; serverUrl:string; size?:number; isResident?:boolean}) {
  const [photo,setPhoto] = useState<string|null>(PHOTO_CACHE[personId]??null);
  useEffect(()=>{
    if(PHOTO_CACHE[personId]){ setPhoto(PHOTO_CACHE[personId]); return; }
    let alive = true;
    AX.get(`${serverUrl||FALLBACK_URL}/persons/${personId}/photo`,{timeout:5000})
      .then(r=>{
        if(alive && r.data?.available && r.data?.photo_b64){
          PHOTO_CACHE[personId] = r.data.photo_b64;
          setPhoto(r.data.photo_b64);
        }
      })
      .catch(()=>{});
    return ()=>{ alive = false; };
  },[personId,serverUrl]);
  const tint = isResident ? C.green : C.blue;
  if(photo) return (
    <Image source={{uri:`data:image/jpeg;base64,${photo}`}}
      style={{width:size,height:size,borderRadius:size/2,borderWidth:2,borderColor:tint}} resizeMode="cover"/>
  );
  return (
    <View style={{width:size,height:size,borderRadius:size/2,backgroundColor:tint+'22',alignItems:'center',justifyContent:'center',borderWidth:2,borderColor:tint+'55'}}>
      <Text style={{color:tint,fontWeight:'700',fontSize:Math.round(size*0.4)}}>{(name??'?').charAt(0).toUpperCase()}</Text>
    </View>
  );
}

function PersonChips({people,selectedId,onPick,serverUrl,allLabel}:
  {people:Person[]; selectedId:string|null; onPick:(p:Person|null)=>void; serverUrl:string; allLabel?:string}) {
  if(people.length===0) return null;
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false}
      contentContainerStyle={{paddingHorizontal:16,gap:10,paddingBottom:10}}>
      {!!allLabel && (
        <TouchableOpacity onPress={()=>onPick(null)}
          style={{paddingHorizontal:14,paddingVertical:10,borderRadius:22,borderWidth:1.5,
                  borderColor:selectedId===null?C.green:C.border,backgroundColor:selectedId===null?C.green+'18':C.card,
                  justifyContent:'center'}}>
          <Text style={{color:selectedId===null?C.green:C.sub,fontWeight:'700',fontSize:13}}>{allLabel}</Text>
        </TouchableOpacity>
      )}
      {people.map(p=>{
        const on = selectedId===p.id;
        return (
          <TouchableOpacity key={p.id} onPress={()=>onPick(p)}
            style={{flexDirection:'row',alignItems:'center',gap:8,paddingLeft:6,paddingRight:14,paddingVertical:6,
                    borderRadius:26,borderWidth:1.5,borderColor:on?C.green:C.border,backgroundColor:on?C.green+'18':C.card}}>
            <PersonAvatar personId={p.id} name={p.name} serverUrl={serverUrl} size={34} isResident={!!p.is_resident}/>
            <View>
              <Text style={{color:C.text,fontWeight:'700',fontSize:13}} numberOfLines={1}>{p.name}</Text>
              <Text style={{color:C.muted,fontSize:10}}>{p.is_resident?'Resident':'Visitor'}</Text>
            </View>
          </TouchableOpacity>
        );
      })}
    </ScrollView>
  );
}

interface InsightItem {
  metric: string; label: string; unit: string;
  current: number; baseline_mean: number; baseline_std: number|null;
  baseline_days: number; delta_pct: number; z_score: number|null;
  direction: 'up'|'down'; adverse: boolean;
  severity: 'normal'|'watch'|'attention'; note: string;
}
interface ResidentInsights {
  person_id: string; name: string;
  status: 'building'|'insufficient_recent'|'ready';
  days_collected: number; days_needed: number;
  recent_days: number; recent_samples: number;
  insights: InsightItem[];
}

// Plain-language help, so a caregiver never needs to know what a baseline
// or a standard deviation is to understand what changed.
const METRIC_INFO:{[k:string]:{what:string;up:string;down:string}} = {
  avg_speed:{what:"How fast they walk. Walking slower than usual can come with tiredness, pain, weakness or illness.",up:"faster",down:"slower"},
  avg_cadence:{what:"Steps per minute. Fewer steps than usual often means more cautious or tired walking.",up:"more steps per minute",down:"fewer steps per minute"},
  avg_posture:{what:"How upright they stand and walk, scored 0 to 100. Lower means more leaning or slouching, which can come with fatigue, pain or balance problems.",up:"more upright",down:"less upright"},
  avg_asymmetry:{what:"How differently the left and right steps move. Higher means more uneven walking, which can come with pain or weakness on one side.",up:"more uneven",down:"more even"},
  resting_hr:{what:"Heart rate while resting, from Apple Watch. Higher than usual can come with illness, stress, dehydration or poor sleep.",up:"higher",down:"lower"},
  hrv_sdnn:{what:"Heart rate variability, from Apple Watch. Lower than usual is a general sign the body is under strain.",up:"higher",down:"lower"},
  spo2_avg:{what:"Blood oxygen, from Apple Watch. A sustained drop can mean breathing problems and is worth checking.",up:"higher",down:"lower"},
  respiratory_rate:{what:"Breaths per minute during sleep. Higher than usual can be an early sign of illness.",up:"higher",down:"lower"},
  wrist_temp:{what:"Skin temperature at night. Higher than usual can come with fever.",up:"higher",down:"lower"},
  sleep_hours:{what:"Hours of sleep. Less than usual can come with pain, worry or illness.",up:"more",down:"less"},
  step_count:{what:"Daily steps. Fewer than usual can mean they are less active or unwell.",up:"more",down:"fewer"},
  activity_level_pct:{what:"Share of the day spent moving, measured by the camera.",up:"more",down:"less"},
  longest_stillness_min:{what:"The longest stretch without moving while in view of the camera.",up:"longer",down:"shorter"},
};

function metricWhat(key:string):string{
  return METRIC_INFO[key]?.what ?? '';
}

function fmtMetric(metric:string,v:any):string{
  const n=Number(v);
  if(!isFinite(n)) return String(v);
  const dec:{[k:string]:number}={avg_speed:2,avg_cadence:0,avg_posture:0,avg_asymmetry:1};
  const d=dec[metric];
  return d===undefined ? String(Math.round(n*100)/100) : n.toFixed(d);
}

function plainChange(metric:string,pct:any,dir:string,base:any,unit:string):string{
  base = fmtMetric(metric, base);
  const p = Math.abs(Math.round(Number(pct)||0));
  if(p<3) return `About the same as usual (${base} ${unit})`;
  const info = METRIC_INFO[metric];
  const word = info ? (dir==='up'?info.up:info.down) : (dir==='up'?'higher':'lower');
  return `${p}% ${word} than usual (usually ${base} ${unit})`;
}

function plainRange(z:any):string{
  if(z===null||z===undefined) return 'Not enough history yet to say how unusual this is';
  const a = Math.abs(Number(z));
  if(a<1.5) return 'Within their normal day-to-day range';
  if(a<2) return 'Outside their usual range';
  return 'Well outside their usual range';
}

function AIInsightsScreen({serverUrl,demoMode,people=[]}:{serverUrl:string;demoMode?:boolean;people?:Person[]}) {
  const [residents,setResidents] = useState<ResidentInsights[]>([]);
  const [pick,setPick] = useState<string|null|undefined>(undefined);   // undefined = not chosen yet
  const named = people.filter(p=>!!p.name);
  const selId = pick===undefined ? (named.find(p=>!!p.is_resident)?.id ?? null) : pick;
  const [disclaimer,setDisclaimer] = useState<string>('');
  const [genAt,setGenAt]     = useState<string|null>(null);
  const [loading,setLoading] = useState(true);
  const [refreshing,setRefreshing] = useState(false);
  const [error,setError]     = useState<string|null>(null);
  const [face,setFace]       = useState<any>(null);
  const [sig,setSig]         = useState<any>(null);
  const [rout,setRout]       = useState<any>(null);
  const [pat,setPat]         = useState<any>(null);
  const [week,setWeek]       = useState<any>(null);

  const load = useCallback(async(quiet?:boolean)=>{
    if(demoMode){
      setResidents(DEMO_INSIGHTS.residents as any);
      setDisclaimer(DEMO_INSIGHTS.disclaimer);
      setGenAt(DEMO_INSIGHTS.generated_at);
      setSig(DEMO_SIGNALS); setFace(DEMO_FACE); setRout(DEMO_ROUTINE); setPat(DEMO_PATTERNS); setWeek(DEMO_WEEK);
      setLoading(false); setRefreshing(false); setError(null);
      return;
    }
    if(!quiet) setLoading(true);
    setError(null);
    try{
      const r = await AX.get(`${serverUrl||FALLBACK_URL}/insights/latest`,{timeout:8000});
      setResidents(r.data?.residents ?? []);
      setDisclaimer(r.data?.disclaimer ?? '');
      setGenAt(r.data?.generated_at ?? null);
      try{
        const f = await AX.get(`${serverUrl||FALLBACK_URL}/face/latest`,{timeout:6000});
        setFace(f.data ?? null);
      }catch(_e){ setFace(null); }   // optional: absence must not break insights
      try{
        const s = await AX.get(`${serverUrl||FALLBACK_URL}/signals/latest`,{timeout:6000});
        setSig(s.data ?? null);
      }catch(_e){ setSig(null); }
      try{
        const rt = await AX.get(`${serverUrl||FALLBACK_URL}/routine/latest`,{timeout:6000});
        setRout(rt.data ?? null);
      }catch(_e){ setRout(null); }
      try{
        const pt = await AX.get(`${serverUrl||FALLBACK_URL}/patterns/latest`,{timeout:6000});
        setPat(pt.data ?? null);
      }catch(_e){ setPat(null); }
      try{
        const wk = await AX.get(`${serverUrl||FALLBACK_URL}/summary/weekly`,{timeout:6000});
        setWeek(wk.data ?? null);
      }catch(_e){ setWeek(null); }
    }catch(e:any){ setError(e?.message ?? 'Request failed'); }
    finally{ setLoading(false); setRefreshing(false); }
  },[serverUrl,demoMode]);

  useEffect(()=>{load();},[load]);
  const onRefresh = useCallback(()=>{ setRefreshing(true); load(true); },[load]);

  const sevColor = (s:string)=> s==='attention'?C.red : s==='watch'?C.orange : C.green;
  const sevLabel = (s:string)=> s==='attention'?'ATTENTION' : s==='watch'?'WATCH' : 'NORMAL';

  if(loading) return (
    <View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg}}>
      <ActivityIndicator color={C.green} size="large"/>
      <Text style={{color:C.sub,marginTop:12}}>Loading insights...</Text>
    </View>
  );

  if(error) return (
    <View style={{flex:1,alignItems:'center',justifyContent:'center',backgroundColor:C.bg,padding:32}}>
      <Text style={{color:C.text,fontSize:18,fontWeight:'700',marginBottom:8}}>Cannot reach server</Text>
      <Text style={{color:C.sub,fontSize:13,textAlign:'center',marginBottom:16}}>{error}</Text>
      <TouchableOpacity style={{backgroundColor:C.blue,paddingHorizontal:24,paddingVertical:12,borderRadius:12}} onPress={()=>load()}>
        <Text style={{color:'#fff',fontWeight:'700',fontSize:14}}>Retry</Text>
      </TouchableOpacity>
    </View>
  );

  return (
    <ScrollView
      style={{flex:1,backgroundColor:C.bg}}
      showsVerticalScrollIndicator={false}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green}/>}>

      <View style={{paddingHorizontal:20,paddingTop:20,paddingBottom:12}}>
        <Text style={{color:C.text,fontSize:34,fontWeight:'800'}}>AI Insights</Text>
        <Text style={{color:C.sub,fontSize:14,marginTop:2}}>
          Measured against each resident's own baseline
        </Text>
      </View>
      <PersonChips people={named} selectedId={selId} serverUrl={serverUrl} allLabel="Everyone"
        onPick={p=>setPick(p?p.id:null)}/>
      <Text style={{color:C.muted,fontSize:12,lineHeight:17,marginHorizontal:20,marginBottom:12}}>
        Health measures below belong to the person you pick. Routine, signals and the weekly summary further down describe the whole room.
      </Text>

      {residents.length===0 && (
        <View style={{alignItems:'center',padding:32}}>
          <Text style={{color:C.text,fontSize:16,fontWeight:'700'}}>No residents yet</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6,textAlign:'center'}}>
            Identify a resident from the camera view to start building insights.
          </Text>
        </View>
      )}

      {residents.filter(r=>selId===null||r.person_id===selId).map(res=>{
        const attention = res.insights.filter(i=>i.severity==='attention').length;
        const watch     = res.insights.filter(i=>i.severity==='watch').length;
        const normal    = res.insights.filter(i=>i.severity==='normal').length;
        const pct = Math.max(0, Math.min(100, (res.days_collected/Math.max(1,res.days_needed))*100));

        return (
          <View key={res.person_id} style={{marginBottom:18}}>
            <View style={{flexDirection:'row',alignItems:'center',gap:12,marginHorizontal:20,marginBottom:10}}>
              <PersonAvatar personId={res.person_id} name={res.name} serverUrl={serverUrl} size={40}
                isResident={!!people.find(p=>p.id===res.person_id)?.is_resident}/>
              <View>
                <Text style={{color:C.text,fontSize:18,fontWeight:'700'}}>{res.name}</Text>
                <Text style={{color:C.muted,fontSize:11}}>
                  {people.find(p=>p.id===res.person_id)?.is_resident?'Resident':'Visitor'}
                </Text>
              </View>
            </View>

            {res.status==='building' && (
              <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,padding:18}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Building baseline</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
                  VIGIL learns {res.name}'s normal walking pattern before it can flag
                  meaningful change. {res.days_collected} of {res.days_needed} days collected.
                </Text>
                <View style={{height:8,backgroundColor:C.fill,borderRadius:4,marginTop:14,overflow:'hidden'}}>
                  <View style={{height:8,width:`${pct}%`,backgroundColor:C.green,borderRadius:4}}/>
                </View>
                <Text style={{color:C.muted,fontSize:11,marginTop:8}}>
                  {res.recent_samples} {res.recent_samples===1?'reading':'readings'} in the last {res.recent_days} days
                </Text>
              </View>
            )}

            {res.status==='insufficient_recent' && (
              <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,padding:18}}>
                <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Not enough recent activity</Text>
                <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
                  A baseline exists, but only {res.recent_samples} readings were recorded in the
                  last {res.recent_days} days. Insights resume once {res.name} is seen walking again.
                </Text>
              </View>
            )}

            {res.status==='ready' && (
              <>
                <View style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,borderRadius:16,padding:16,flexDirection:'row'}}>
                  {[['ATTENTION',attention,C.red],['WATCH',watch,C.orange],['NORMAL',normal,C.green]].map(([l,n,c])=>(
                    <View key={l as string} style={{flex:1,alignItems:'center'}}>
                      <Text style={{color:c as string,fontSize:28,fontWeight:'800'}}>{String(n)}</Text>
                      <Text style={{color:C.sub,fontSize:11,marginTop:2}}>{l as string}</Text>
                    </View>
                  ))}
                </View>

                {res.insights.map(it=>{
                  const col = sevColor(it.severity);
                  const arrow = it.direction==='up' ? '\u2191' : '\u2193';
                  return (
                    <View key={it.metric} style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,
                                                  borderRadius:16,padding:16,borderLeftWidth:3,borderLeftColor:col}}>
                      <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                        <Text style={{color:C.text,fontSize:15,fontWeight:'700',flex:1,marginRight:8}}>{it.label}</Text>
                        <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                      paddingHorizontal:8,paddingVertical:3,borderRadius:8}}>
                          <Text style={{color:col,fontSize:10,fontWeight:'800',letterSpacing:0.5}}>
                            {sevLabel(it.severity)}
                          </Text>
                        </View>
                      </View>

                      <Text style={{color:C.text,fontSize:28,fontWeight:'800',marginTop:8,
                                    fontVariant:['tabular-nums']}}>
                        {fmtMetric(it.metric,it.current)}<Text style={{color:C.sub,fontSize:15,fontWeight:'600'}}> {it.unit}</Text>
                      </Text>

                      <Text style={{color:C.sub,fontSize:13,marginTop:4}}>
                        {arrow} {plainChange(it.metric,it.delta_pct,it.direction,it.baseline_mean,it.unit)}
                      </Text>
                      {it.z_score!==null && (
                        <Text style={{color:C.muted,fontSize:11,marginTop:3}}>
                          {plainRange(it.z_score)}
                          {it.baseline_days?` \u00b7 ${it.baseline_days}-day baseline`:''}
                        </Text>
                      )}

                      <View style={{height:1,backgroundColor:C.border,marginVertical:12}}/>
                      <Text style={{color:C.text,fontSize:12,lineHeight:18,marginBottom:6}}>{metricWhat(it.metric)}</Text>
                      <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>{it.note}</Text>
                    </View>
                  );
                })}
              </>
            )}
          </View>
        );
      })}

      {!!week && Array.isArray(week.lines) && week.lines.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
            <View style={{flex:1,marginRight:10}}>
              <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>This Week</Text>
              {!!week.period&&<Text style={{color:C.muted,fontSize:11,marginTop:2}}>{week.period}</Text>}
            </View>
            <TouchableOpacity onPress={()=>{Share.share({message:week.text}).catch(()=>{});}}
              style={{paddingHorizontal:12,paddingVertical:7,borderRadius:10,
                      borderWidth:1,borderColor:C.blue+'66'}}>
              <Text style={{color:C.blue,fontSize:12,fontWeight:'700'}}>Share with family</Text>
            </TouchableOpacity>
          </View>
          {week.lines.map((l:string,i:number)=>(
            <Text key={i} style={{color:C.sub,fontSize:13,lineHeight:20,marginBottom:4}}>{l}</Text>
          ))}
        </View>
      )}

      {!!pat && Array.isArray(pat.hourly) && pat.hourly.length===24 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Activity Pattern</Text>
          <Text style={{color:C.sub,fontSize:12,marginTop:4,marginBottom:14}}>
            When {pat.days_of_history?`${pat.days_of_history} days of history show`:'this resident is'} activity, hour by hour
          </Text>

          {(()=>{
            const peak = Math.max(1, ...pat.hourly.map((h:any)=>Math.max(h.avg_minutes,h.today_minutes)));
            return (
              <View style={{flexDirection:'row',alignItems:'flex-end',height:84,gap:2}}>
                {pat.hourly.map((h:any)=>{
                  const night = h.hour>=22||h.hour<6;
                  const ah = Math.max(2,(h.avg_minutes/peak)*72);
                  const th = Math.max(0,(h.today_minutes/peak)*72);
                  return (
                    <View key={h.hour} style={{flex:1,alignItems:'center',justifyContent:'flex-end',height:84}}>
                      <View style={{width:'100%',height:ah,backgroundColor:night?C.blue+'44':C.fill,
                                    borderRadius:2,justifyContent:'flex-end'}}>
                        {th>0&&(
                          <View style={{width:'100%',height:Math.min(th,ah),
                                        backgroundColor:night?C.orange:C.green,borderRadius:2}}/>
                        )}
                      </View>
                    </View>
                  );
                })}
              </View>
            );
          })()}

          <View style={{flexDirection:'row',justifyContent:'space-between',marginTop:6}}>
            {['12a','6a','12p','6p','11p'].map(l=>(
              <Text key={l} style={{color:C.muted,fontSize:10}}>{l}</Text>
            ))}
          </View>

          <View style={{flexDirection:'row',gap:14,marginTop:10,alignItems:'center'}}>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.fill}}/>
              <Text style={{color:C.muted,fontSize:11}}>usual</Text>
            </View>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.green}}/>
              <Text style={{color:C.muted,fontSize:11}}>today</Text>
            </View>
            <View style={{flexDirection:'row',alignItems:'center',gap:5}}>
              <View style={{width:9,height:9,borderRadius:2,backgroundColor:C.blue+'44'}}/>
              <Text style={{color:C.muted,fontSize:11}}>night hours</Text>
            </View>
          </View>

          {pat.night_minutes_today>pat.night_minutes_avg*2&&pat.night_minutes_today>10&&(
            <View style={{marginTop:14,backgroundColor:C.orange+'18',borderRadius:10,padding:12}}>
              <Text style={{color:C.orange,fontSize:12,fontWeight:'700',marginBottom:3}}>
                More night movement than usual
              </Text>
              <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
                {Math.round(pat.night_minutes_today)} minutes active between 10pm and 6am,
                against a usual {Math.round(pat.night_minutes_avg)}. Falls often happen
                getting out of bed at night.
              </Text>
            </View>
          )}

          {Array.isArray(pat.anomalies)&&pat.anomalies.length>0&&(
            <View style={{marginTop:14}}>
              <Text style={{color:C.sub,fontSize:11,fontWeight:'700',letterSpacing:0.5,
                            textTransform:'uppercase',marginBottom:8}}>Unusual days</Text>
              {pat.anomalies.slice(0,4).map((a:any)=>(
                <View key={a.date} style={{flexDirection:'row',justifyContent:'space-between',
                                           paddingVertical:5}}>
                  <Text style={{color:C.text,fontSize:13}}>{a.date}</Text>
                  <Text style={{color:a.score>=90?C.orange:C.sub,fontSize:13,
                                fontWeight:'600',fontVariant:['tabular-nums']}}>
                    {a.score}
                  </Text>
                </View>
              ))}
              <Text style={{color:C.muted,fontSize:11,lineHeight:16,marginTop:8}}>
                {pat.method}
              </Text>
            </View>
          )}
        </View>
      )}

      {!!rout && Array.isArray(rout.items) && rout.items.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:6}}>Daily Routine</Text>
          <Text style={{color:rout.flagged_count>=2?C.orange:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
            {rout.summary}
          </Text>

          {rout.items.map((it:any)=>{
            if(it.status!=='ready'){
              return (
                <View key={it.label} style={{marginBottom:12,paddingBottom:12,
                                             borderBottomWidth:1,borderBottomColor:C.border}}>
                  <Text style={{color:C.muted,fontSize:14,fontWeight:'600'}}>{it.label}</Text>
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Learning this pattern \u2014 {it.days_of_history} of {it.days_needed} days
                  </Text>
                </View>
              );
            }
            const low = it.stability==='low';
            const col = it.severity==='attention'?C.red:it.severity==='watch'?C.orange:C.sub;
            const shown = it.current_text ?? it.current;
            const base  = it.baseline_text ?? it.baseline;
            return (
              <View key={it.label} style={{marginBottom:12,paddingBottom:12,
                                           borderBottomWidth:1,borderBottomColor:C.border}}>
                <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                  <Text style={{color:low?C.muted:C.text,fontSize:14,fontWeight:'600',flex:1,marginRight:8}}>
                    {it.label}
                  </Text>
                  {it.severity!=='normal'&&(
                    <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                  paddingHorizontal:8,paddingVertical:2,borderRadius:7}}>
                      <Text style={{color:col,fontSize:9,fontWeight:'800',letterSpacing:0.5}}>
                        {it.severity==='attention'?'ATTENTION':'WATCH'}
                      </Text>
                    </View>
                  )}
                </View>

                <Text style={{color:low?C.muted:C.text,fontSize:20,fontWeight:'700',marginTop:4,
                              fontVariant:['tabular-nums']}}>
                  {shown}
                  {!it.current_text&&<Text style={{color:C.sub,fontSize:13,fontWeight:'600'}}> {it.unit}</Text>}
                </Text>

                <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
                  usually {base}{it.current_text?'':` ${it.unit}`}
                  {it.days_of_history?` \u00b7 ${it.days_of_history} days`:''}
                </Text>

                {low && (
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Varies too much day to day to flag reliably
                  </Text>
                )}
                {!low && it.severity!=='normal' && (
                  <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginTop:8}}>{it.note}</Text>
                )}
              </View>
            );
          })}

          {!!rout.disclaimer && (
            <Text style={{color:C.muted,fontSize:11,lineHeight:16}}>{rout.disclaimer}</Text>
          )}
        </View>
      )}

      {!!sig && Array.isArray(sig.signals) && sig.signals.length>0 && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700',marginBottom:6}}>Health Signals</Text>
          <Text style={{color:sig.flagged_count>=2?C.orange:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
            {sig.summary}
          </Text>

          {sig.signals.map((s:any)=>{
            const low = s.baseline_stability==='low';
            const col = s.severity==='attention'?C.red:s.severity==='watch'?C.orange:C.sub;
            const arrow = s.direction==='up'?'\u2191':'\u2193';
            return (
              <View key={s.metric} style={{marginBottom:12,paddingBottom:12,
                                           borderBottomWidth:1,borderBottomColor:C.border}}>
                <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
                  <Text style={{color:low?C.muted:C.text,fontSize:14,fontWeight:'600',flex:1,marginRight:8}}>
                    {s.label}
                  </Text>
                  {s.severity!=='normal'&&(
                    <View style={{backgroundColor:col+'22',borderColor:col+'55',borderWidth:1,
                                  paddingHorizontal:8,paddingVertical:2,borderRadius:7}}>
                      <Text style={{color:col,fontSize:9,fontWeight:'800',letterSpacing:0.5}}>
                        {s.severity==='attention'?'ATTENTION':'WATCH'}
                      </Text>
                    </View>
                  )}
                </View>

                <Text style={{color:low?C.muted:C.text,fontSize:20,fontWeight:'700',marginTop:4,
                              fontVariant:['tabular-nums']}}>
                  {s.current}<Text style={{color:C.sub,fontSize:13,fontWeight:'600'}}> {s.unit}</Text>
                </Text>

                <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
                  <Text style={{color:C.muted}}>{metricWhat(s.metric)}{'\n'}</Text>{arrow} {plainChange(s.metric,s.delta_pct,s.direction,s.baseline_mean,s.unit)}
                  {s.baseline_days?` \u00b7 ${s.baseline_days} days`:''}
                </Text>

                {low && (
                  <Text style={{color:C.muted,fontSize:11,marginTop:4}}>
                    Limited history — this measure varies too much day to day to flag reliably
                  </Text>
                )}
                {!low && s.severity!=='normal' && (
                  <Text style={{color:C.sub,fontSize:12,lineHeight:18,marginTop:8}}>{s.note}</Text>
                )}
              </View>
            );
          })}

          {!!sig.disclaimer && (
            <Text style={{color:C.muted,fontSize:11,lineHeight:16,marginTop:2}}>
              {sig.disclaimer}
            </Text>
          )}
        </View>
      )}

      {!!sig && sig.status!=='ready' && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Health Signals</Text>
          <Text style={{color:C.sub,fontSize:13,marginTop:6,lineHeight:19}}>
            Collecting daily history. Physiological measures need about 10 days
            before a personal norm can be established.
          </Text>
        </View>
      )}

      {!!face && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:16,padding:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>Facial Cues</Text>
            <View style={{backgroundColor:C.blue+'22',borderColor:C.blue+'55',borderWidth:1,
                          paddingHorizontal:8,paddingVertical:3,borderRadius:8}}>
              <Text style={{color:C.blue,fontSize:10,fontWeight:'800',letterSpacing:0.5}}>EXPERIMENTAL</Text>
            </View>
          </View>

          <View style={{flexDirection:'row'}}>
            <View style={{flex:1}}>
              <Text style={{color:C.text,fontSize:28,fontWeight:'800',fontVariant:['tabular-nums']}}>
                {face.yawns_today ?? 0}
              </Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:2}}>Yawns today</Text>
            </View>
            <View style={{flex:1}}>
              <Text style={{color:C.text,fontSize:28,fontWeight:'800',fontVariant:['tabular-nums']}}>
                {face.perclos_1h_avg!=null?`${Math.round(face.perclos_1h_avg*100)}%`:'--'}
              </Text>
              <Text style={{color:C.sub,fontSize:12,marginTop:2}}>Eyes closed, last hour</Text>
            </View>
          </View>

          <View style={{height:1,backgroundColor:C.border,marginVertical:12}}/>
          <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>
            Yawn frequency and eye-closure time are established fatigue indicators.
            VIGIL is still collecting enough history to compare these against this
            resident's own norm, so treat single readings as observations rather
            than conclusions.
          </Text>
          {face.perclos_samples!=null&&(
            <Text style={{color:C.muted,fontSize:11,marginTop:8}}>
              {face.perclos_samples} eye-closure samples in the last hour
            </Text>
          )}
        </View>
      )}

      {!!disclaimer && (
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:12,
                      borderWidth:1,borderColor:C.fill,padding:14}}>
          <Text style={{color:C.orange,fontSize:11,fontWeight:'700',letterSpacing:0.5,
                        marginBottom:6,textTransform:'uppercase'}}>Wellness Monitoring Only</Text>
          <Text style={{color:C.sub,fontSize:12,lineHeight:18}}>{disclaimer}</Text>
        </View>
      )}

      {genAt && (
        <Text style={{color:C.muted,fontSize:11,textAlign:'center',marginBottom:24}}>
          {'Updated '+timeAgo(genAt)}
        </Text>
      )}
      <View style={{height:24}}/>
    </ScrollView>
  );
}

// ─────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────

// ─────────────────────────────────────────────
// DEMO MODE
// Apple requires a fully-featured demo mode when an app depends on
// hardware a reviewer does not have. Values below mirror this system's
// real measured output so the demo is representative, not invented.
// ─────────────────────────────────────────────
const DEMO_RESIDENT = 'Margaret (Demo Resident)';

function demoDate(back:number):string{
  const d=new Date(); d.setDate(d.getDate()-back);
  return d.toISOString().slice(0,10);
}
function demoIso(hoursAgo:number):string{
  return new Date(Date.now()-hoursAgo*3600*1000).toISOString();
}


// Person detail demo data. The final days drift downward on purpose so
// the detail screen shows the kind of change VIGIL exists to surface.
const DEMO_PTRENDS:PersonTrendDay[] = Array.from({length:14},(_,i):PersonTrendDay=>{
  const drift = i>=10 ? (i-9)*0.035 : 0;
  const w=(n:number,a:number)=>+(n+((i%3)-1)*a).toFixed(2);
  return {
    date: demoDate(13-i),
    avg_speed: +(w(1.14,0.04)-drift).toFixed(2),
    avg_cadence: +(w(95.7,2.6)-drift*40).toFixed(1),
    avg_posture: w(43.7,3.2),
    avg_asymmetry: +(w(4.6,0.6)+drift*8).toFixed(1),
    samples: Math.round(w(180,25)),
  };
});

const DEMO_PBASELINE:PersonBaselineStatus = {
  person_id:'demo_1', has_baseline:true, days_collected:14, days_needed:7,
  baseline:{
    avg_speed:{mean:1.131,std:0.06},
    avg_cadence:{mean:95.66,std:3.37},
    avg_posture:{mean:43.68,std:13.45},
    avg_asymmetry:{mean:4.62,std:0.71},
  },
};

const DEMO_WEEKLY = {
  this_week:{avg_speed:1.04,avg_cadence:91.8,avg_posture:42.1,avg_asymmetry:5.8},
  last_week:{avg_speed:1.15,avg_cadence:96.4,avg_posture:44.2,avg_asymmetry:4.5},
  change_pct:{avg_speed:-9.6,avg_cadence:-4.8,avg_posture:-4.8,avg_asymmetry:28.9},
};

const DEMO_TODAY:DailySummary = {
  date: demoDate(0), step_count: 3820, hrv_sdnn: 38.4, resting_hr: 74,
  sleep_hours: 6.4, active_calories: 210, spo2_avg: 96.8,
  walking_asymmetry_pct: 5.4, walking_speed_ms: 1.04,
  camera_gait_speed_ms: 1.01, camera_cadence_spm: 90.2,
  camera_asymmetry_pct: 6.1, posture_score: 41.8,
  active_minutes: 168, still_minutes: 430, activity_level_pct: 28.1,
  longest_stillness_min: 61, camera_data_points: 1840, anomaly_flag: 1,
};

const DEMO_HISTORY:DailySummary[] = Array.from({length:14},(_,i):DailySummary=>{
  const w=(n:number,a:number)=>+(n+((i%3)-1)*a).toFixed(2);
  return {
    date: demoDate(13-i),
    step_count: Math.round(w(4300,420)),
    hrv_sdnn: w(41,3.1), resting_hr: Math.round(w(68,2)),
    sleep_hours: w(7.1,0.5), active_calories: Math.round(w(240,30)),
    spo2_avg: w(97.2,0.4), walking_asymmetry_pct: w(3.8,0.6),
    walking_speed_ms: w(1.14,0.05), camera_gait_speed_ms: w(1.13,0.05),
    camera_cadence_spm: w(95.7,3.2), camera_asymmetry_pct: w(4.6,0.7),
    posture_score: w(43.7,4.0), active_minutes: Math.round(w(214,26)),
    still_minutes: Math.round(w(372,40)), activity_level_pct: w(36.5,5),
    longest_stillness_min: Math.round(w(42,9)),
    camera_data_points: Math.round(w(2100,300)), anomaly_flag: 0,
  };
});

const DEMO_ALERTS:AlertEntry[] = [
  { id:3, alert_type:'bed_exit', detected_at:demoIso(9), created_at:demoIso(9), seen:1,
    payload:{rested_min:142, hour:2,
             message:'Got up during the night after resting about 142 minutes'} },
  { id:2, alert_type:'fall', detected_at:demoIso(3), created_at:demoIso(3), seen:0,
    payload:{hip_drop_pct:1.0,torso_angle:78.4,aspect:3.08,
             message:'Fall detected by camera',resident_name:DEMO_RESIDENT} },
  { id:1, alert_type:'health_signal', detected_at:demoIso(26), created_at:demoIso(26), seen:1,
    payload:{metric:'avg_speed',
             message:'Walking speed 11% below 14-day baseline'} },
];

const DEMO_PERSONS:Person[] = [
  { id:'demo_1', name:DEMO_RESIDENT, room_id:'room_1', is_resident:1,
    height_px:742.3, confidence:0.92, frames_seen:18420,
    first_seen_at:demoIso(24*14), last_seen_at:demoIso(0.2) },
];

const DEMO_CAMERA:CameraStatus = {
  online:true, fps:11.6, person_present:true, person_count:1,
  last_seen_minutes_ago:0, camera_mode:'live',
};

const DEMO_ROOMS:Room[] = [
  { id:'room_1', name:'Room 1', resident_name:DEMO_RESIDENT,
    emergency_phone:'+1 (555) 010-0000', camera_online:true,
    person_present:true, person_count:1, fps:11.6 },
];

const DEMO_INSIGHTS = {
  generated_at:new Date().toISOString(),
  residents:[{
    person_id:'demo_1', name:DEMO_RESIDENT, status:'ready',
    days_collected:14, days_needed:7, recent_days:3, recent_samples:1620,
    insights:[
      { metric:'avg_speed', label:'Walking Speed', unit:'m/s', current:1.01,
        baseline_mean:1.131, baseline_std:0.06, baseline_days:14,
        delta_pct:-10.7, z_score:-2.02, direction:'down', adverse:true,
        severity:'attention',
        note:"Walking speed is a widely used functional measure in older adults. "+
             "A sustained decline relative to a person's own norm is associated "+
             "with increased fall risk." },
      { metric:'avg_cadence', label:'Cadence', unit:'steps/min', current:90.2,
        baseline_mean:95.66, baseline_std:3.37, baseline_days:14,
        delta_pct:-5.7, z_score:-1.62, direction:'down', adverse:true,
        severity:'watch',
        note:"Cadence below a person's own norm can accompany fatigue, pain, "+
             "or reduced walking confidence." },
      { metric:'avg_posture', label:'Posture Score', unit:'/100', current:41.8,
        baseline_mean:43.68, baseline_std:13.45, baseline_days:14,
        delta_pct:-4.3, z_score:-0.14, direction:'down', adverse:true,
        severity:'normal',
        note:"Lower scores reflect greater trunk lean or shoulder asymmetry." },
    ],
    convergence:{ adverse_count:3, total_metrics:3, attention:1, watch:1,
      summary:"Multiple signals have moved away from this person's normal at "+
              "the same time. Convergence across several measures is more "+
              "meaningful than any one reading." },
  }],
  disclaimer:"VIGIL reports measured changes against each resident's own "+
             "baseline. It does not diagnose conditions and is not a medical device.",
};

const DEMO_SIGNALS = {
  status:'ready', generated_at:new Date().toISOString(),
  baseline_days:21, recent_days:3, flagged_count:2,
  summary:'2 signals have moved away from normal at the same time. '+
          'Convergence across several measures is more meaningful than any '+
          'single reading.',
  signals:[
    { metric:'resting_hr', label:'Resting Heart Rate', unit:'bpm', current:74.0,
      baseline_mean:68.2, baseline_std:2.1, baseline_days:18, baseline_cv:0.03,
      baseline_stability:'ok', delta_pct:8.5, z_score:2.76, direction:'up',
      adverse:true, severity:'attention',
      note:"A resting heart rate above a person's own norm can accompany "+
           "infection, dehydration, pain or poor sleep. It often rises before "+
           "other symptoms appear." },
    { metric:'sleep_hours', label:'Sleep', unit:'hours', current:6.4,
      baseline_mean:7.14, baseline_std:0.42, baseline_days:18, baseline_cv:0.06,
      baseline_stability:'ok', delta_pct:-10.4, z_score:-1.76, direction:'down',
      adverse:true, severity:'watch',
      note:"Reduced sleep can accompany pain, anxiety, breathing difficulty "+
           "or illness." },
    { metric:'spo2_avg', label:'Blood Oxygen', unit:'%', current:96.8,
      baseline_mean:97.2, baseline_std:0.38, baseline_days:18, baseline_cv:0.00,
      baseline_stability:'ok', delta_pct:-0.4, z_score:-1.05, direction:'down',
      adverse:true, severity:'normal',
      note:"A sustained fall in blood oxygen saturation can indicate "+
           "respiratory compromise." },
  ],
  disclaimer:"These are deviations from this resident's own recent norm, not "+
             "diagnoses. VIGIL does not identify medical conditions and is not "+
             "a medical device.",
};

const DEMO_WEEK_LINES = [
  'Walking speed averaged 1.04 m/s, down 9% from last week.',
  'Cadence averaged 92 steps/min, down 4% from last week.',
  'About 3.1 hours active per day, down 12% from last week.',
  'Night-time activity: 6 episodes this week (2 last week).',
  'Alerts this week: 1 fall, 1 night bed exit.',
  'Median time to acknowledge an alert: 3 min.',
];
const DEMO_WEEK = {
  period:'Last 7 days', name:DEMO_RESIDENT, lines:DEMO_WEEK_LINES,
  text:'VIGIL weekly summary for '+DEMO_RESIDENT+' (last 7 days)\n\n'+
       DEMO_WEEK_LINES.join('\n')+
       '\n\nVIGIL compares each resident with their own history. It does not diagnose conditions.',
};

const DEMO_PATTERNS = {
  generated_at:new Date().toISOString(), days_of_history:21, days_scored:78,
  busiest_hours:[9,14,19], night_minutes_avg:4.2, night_minutes_today:31.5,
  hourly:[0,0,0,0,0,2,14,38,52,61,44,33,29,41,58,47,35,40,56,62,38,19,6,1]
    .map((v,h)=>({hour:h, avg_minutes:v,
                  today_minutes: h>=0&&h<6 ? (h===2?12:h===3?19:0) : Math.max(0,v-8),
                  days_with_activity: v>0?21:0})),
  anomalies:[{date:'2026-09-26',score:88.4},{date:'2026-09-19',score:74.1}],
  method:"Unusual days are identified by an isolation forest trained only on "+
         "this resident's own daily history.",
  disclaimer:"An unusual day is an observation, not a diagnosis. It indicates "+
             "the day differed from this resident's pattern, not why.",
};

const DEMO_ROUTINE = {
  status:'ready', generated_at:new Date().toISOString(), flagged_count:1,
  summary:'Night Activity is outside its usual range.',
  items:[
    { label:'Morning Start', status:'ready', unit:'minutes after midnight',
      current:498, baseline:432, spread:28, stability:'ok', spread_ratio:0.06,
      current_text:'08:18', baseline_text:'07:12', days_of_history:21,
      z_score:2.36, severity:'watch',
      note:'A later start than usual can reflect poor sleep, pain, low mood or illness. It is one of the earliest observable changes in daily routine.' },
    { label:'Night Activity', status:'ready', unit:'episodes',
      current:4, baseline:1, spread:1, stability:'ok', spread_ratio:1.0,
      days_of_history:21, z_score:3.0, severity:'attention',
      note:'Movement between 22:00 and 06:00. This matters for safety: many falls happen getting out of bed at night. More night waking than usual can also accompany a urinary issue, a medication side effect, or disturbed sleep.' },
    { label:'Night Time Active', status:'ready', unit:'minutes',
      current:38, baseline:12, spread:6, stability:'ok', spread_ratio:0.5,
      days_of_history:21, z_score:4.3, severity:'attention',
      note:'Total time moving overnight. Separates one long episode from several brief ones.' },
    { label:'Longest Daytime Absence', status:'ready', unit:'minutes',
      current:42, baseline:38, spread:14, stability:'ok', spread_ratio:0.37,
      days_of_history:21, z_score:0.29, severity:'normal',
      note:'Time away from the monitored room during the day.' },
  ],
  disclaimer:"Routine changes are observations about this resident's own pattern, not diagnoses.",
};

const DEMO_FACE = {
  yawns_today:4, perclos_1h_avg:0.118, perclos_1h_max:0.21,
  perclos_samples:54, recent:[],
  disclaimer:'Facial cues are wellness signals, not a diagnosis.',
};

// Who is responding. Asked once, saved on this phone, sent with every
// acknowledgement so the record shows a person, not just "someone".
async function caregiverName():Promise<string>{
  try{
    const saved = await AsyncStorage.getItem('vigil_caregiver_name');
    if(saved) return saved;
  }catch(_e){}
  return await new Promise<string>(resolve=>{
    Alert.prompt(
      'Who is responding?',
      'Your name is saved with acknowledgements so the record shows who checked on the resident.',
      [
        {text:'Skip',style:'cancel',onPress:()=>resolve('Caregiver')},
        {text:'Save',onPress:(v?:string)=>{
          const n=(v||'').trim()||'Caregiver';
          AsyncStorage.setItem('vigil_caregiver_name',n).catch(()=>{});
          resolve(n);
        }},
      ],
      'plain-text');
  });
}

// KEYBOARD_CALM_V1: a text field that keeps its own text. Typing redraws
// only this field; the app hears about it 0.2 s after typing pauses, and
// immediately on leaving the field or pressing Return.
const KInput=React.memo(function KInput({value,onChange,onSubmit,liveRef,style,...rest}:any){
  const [t,setT]=useState<string>(value??'');
  const focused=useRef(false);
  const timer=useRef<any>(null);
  const cb=useRef(onChange); cb.current=onChange;
  useEffect(()=>{ if(liveRef) liveRef.current=value??''; },[]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{ if(!focused.current) setT(value??''); },[value]);
  useEffect(()=>()=>{ if(timer.current) clearTimeout(timer.current); },[]);
  const flush=(v:string)=>{ if(timer.current){clearTimeout(timer.current);timer.current=null;} if(cb.current) cb.current(v); };
  return(
    <TextInput {...rest} style={style} value={t}
      onChangeText={(v:string)=>{
        setT(v); if(liveRef) liveRef.current=v;
        if(timer.current) clearTimeout(timer.current);
        timer.current=setTimeout(()=>flush(v),200);
      }}
      onFocus={()=>{focused.current=true;}}
      onBlur={()=>{focused.current=false; flush(t);}}
      onSubmitEditing={(e:any)=>{ const v=e?.nativeEvent?.text??t; flush(v); if(onSubmit) onSubmit(v); }}/>
  );
});

// Drawn tab-bar icons (match the website's line icons).
function TabIcon({name,color}:{name:string;color:string}){
  const p={stroke:color,strokeWidth:2,fill:'none',strokeLinecap:'round' as const,strokeLinejoin:'round' as const};
  return(
    <Svg width={24} height={24} viewBox="0 0 24 24">
      {name==='today'&&<Path d="M3 12h4l3-8 4 16 3-8h4" {...p}/>}
      {name==='trends'&&<><Path d="M3 17l6-6 4 4 8-8" {...p}/><Path d="M15 7h6v6" {...p}/></>}
      {name==='alerts'&&<><Path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" {...p}/><Path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" {...p}/></>}
      {name==='insights'&&<><Path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" {...p}/><Path d="M19 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" {...p}/></>}
      {name==='settings'&&<><Circle cx={12} cy={12} r={3} {...p}/><Path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7" {...p}/></>}
    </Svg>
  );
}

// ROOM_SETUP_V1: draw bed and couch zones on the live picture.
type Zone={x1:number;y1:number;x2:number;y2:number;label?:string};
function ZoneEditor({serverUrl,onClose}:{serverUrl:string;onClose:()=>void}){
  const W=SW-32, H=Math.round((SW-32)*9/16);
  const u=serverUrl||FALLBACK_URL;
  const [frame,setFrame]=useState<string|null>(null);
  const [zones,setZones]=useState<Zone[]>([]);
  const [draft,setDraft]=useState<Zone|null>(null);
  const [label,setLabel]=useState('Bed');
  const [msg,setMsg]=useState<string|null>(null);
  const [saving,setSaving]=useState(false);
  const start=useRef<{x:number;y:number}|null>(null);
  useEffect(()=>{
    AX.get(`${u}/camera/snapshot?w=800`,{timeout:6000})
      .then(r=>{if(r.data?.frame)setFrame(r.data.frame);else setMsg('The camera picture is not available right now.');})
      .catch(()=>setMsg('Could not reach the camera unit.'));
    AX.get(`${u}/zones`,{timeout:6000})
      .then(r=>{if(Array.isArray(r.data?.zones))setZones(r.data.zones);})
      .catch(()=>{});
  },[u]);
  const clamp=(v:number)=>Math.max(0,Math.min(1,v));
  const pr=useRef(PanResponder.create({
    onStartShouldSetPanResponder:()=>true,
    onMoveShouldSetPanResponder:()=>true,
    onPanResponderGrant:(e)=>{
      const {locationX,locationY}=e.nativeEvent;
      const p={x:clamp(locationX/W),y:clamp(locationY/H)};
      start.current=p; setDraft({x1:p.x,y1:p.y,x2:p.x,y2:p.y});
    },
    onPanResponderMove:(_e,g)=>{
      const p=start.current; if(!p)return;
      const x=clamp(p.x+g.dx/W), y=clamp(p.y+g.dy/H);
      setDraft({x1:Math.min(p.x,x),y1:Math.min(p.y,y),x2:Math.max(p.x,x),y2:Math.max(p.y,y)});
    },
    onPanResponderRelease:()=>{start.current=null;},
  })).current;
  const big=(z:Zone|null)=>!!z&&(z.x2-z.x1)>=0.03&&(z.y2-z.y1)>=0.03;
  const box=(z:Zone,key:string,col:string,dashed=false)=>(
    <View key={key} pointerEvents="none" style={{position:'absolute',left:z.x1*W,top:z.y1*H,width:(z.x2-z.x1)*W,height:(z.y2-z.y1)*H,
      borderWidth:2,borderColor:col,borderStyle:dashed?'dashed':'solid',backgroundColor:col+'33',borderRadius:6}}>
      {!!z.label&&<Text style={{color:'#fff',fontSize:11,fontWeight:'800',backgroundColor:col,alignSelf:'flex-start',paddingHorizontal:6,paddingVertical:2,borderBottomRightRadius:6}}>{z.label}</Text>}
    </View>
  );
  const save=async()=>{
    setSaving(true); setMsg(null);
    try{
      const r=await AX.post(`${u}/zones`,{zones},{timeout:8000});
      if(Array.isArray(r.data?.zones))setZones(r.data.zones);
      setMsg('Saved. VIGIL uses these zones within 5 seconds.');
    }catch(_e){ setMsg('Could not save. Check the connection to the camera unit.'); }
    finally{ setSaving(false); }
  };
  return(
    <SafeAreaView style={{flex:1,backgroundColor:C.bg}}>
      <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',paddingHorizontal:16,paddingVertical:12}}>
        <Text style={{color:C.text,fontSize:17,fontWeight:'800'}}>Room setup</Text>
        <TouchableOpacity onPress={onClose} hitSlop={{top:10,bottom:10,left:10,right:10}}><Text style={{color:C.green,fontSize:16,fontWeight:'700'}}>Done</Text></TouchableOpacity>
      </View>
      <Text style={{color:C.sub,fontSize:14,lineHeight:20,marginHorizontal:16,marginBottom:12}}>
        Drag a box over each bed, couch and armchair. Lying down inside a box is never treated as a fall, and night bed exits are watched from the bed.
      </Text>
      <View style={{marginHorizontal:16,width:W,height:H,borderRadius:14,overflow:'hidden',backgroundColor:C.card,borderWidth:1,borderColor:C.border}}>
        {frame
          ?<Image source={{uri:`data:image/jpeg;base64,${frame}`}} style={{width:W,height:H}} resizeMode="cover"/>
          :<View style={{flex:1,alignItems:'center',justifyContent:'center'}}><ActivityIndicator color={C.green}/></View>}
        <View style={{position:'absolute',left:0,top:0,width:W,height:H}} {...pr.panHandlers}>
          {zones.map((z,i)=>box(z,'z'+i,C.green))}
          {draft&&big(draft)&&box({...draft,label},'draft',C.orange,true)}
        </View>
      </View>
      <ScrollView contentContainerStyle={{padding:16,paddingBottom:40}}>
        <View style={{flexDirection:'row',gap:8,marginBottom:12}}>
          {['Bed','Couch','Chair'].map(l=>(
            <TouchableOpacity key={l} onPress={()=>setLabel(l)}
              style={{paddingHorizontal:16,paddingVertical:9,borderRadius:100,borderWidth:1.5,borderColor:label===l?C.green:C.border,backgroundColor:label===l?C.green+'22':'transparent'}}>
              <Text style={{color:label===l?C.green:C.sub,fontWeight:'700'}}>{l}</Text>
            </TouchableOpacity>
          ))}
        </View>
        <TouchableOpacity disabled={!big(draft)} onPress={()=>{if(draft&&big(draft)){setZones(z=>[...z,{...draft,label}].slice(0,6));setDraft(null);}}}
          style={[s.btnSecondary,{opacity:big(draft)?1:0.4}]}>
          <Text style={s.btnSecondaryTxt}>{big(draft)?`Add this box as ${label}`:'Drag on the picture to draw a box'}</Text>
        </TouchableOpacity>
        {zones.length>0&&(
          <View style={[s.settGrp,{marginHorizontal:0,marginTop:16}]}>
            {zones.map((z,i)=>(
              <View key={i} style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center',paddingHorizontal:16,paddingVertical:13,borderTopWidth:i?1:0,borderTopColor:C.hair}}>
                <Text style={{color:C.text,fontSize:15}}>{z.label||'Furniture'}</Text>
                <TouchableOpacity onPress={()=>setZones(zs=>zs.filter((_x,j)=>j!==i))}><Text style={{color:C.red,fontWeight:'700'}}>Remove</Text></TouchableOpacity>
              </View>
            ))}
          </View>
        )}
        <TouchableOpacity onPress={save} disabled={saving} style={[s.btnPrimary,{marginTop:18,opacity:saving?0.6:1}]}>
          <Text style={s.btnPrimaryTxt}>{saving?'Saving...':'Save zones'}</Text>
        </TouchableOpacity>
        {!!msg&&<Text style={{color:C.sub,fontSize:13,textAlign:'center',marginTop:12}}>{msg}</Text>}
        <Text style={{color:C.muted,fontSize:12,lineHeight:18,marginTop:18}}>
          Keep the camera fixed, with automatic tracking turned off. If the camera is moved, redo this screen.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

// Defined outside App so its identity is stable across re-renders;
// inside App, every re-render remounted each text field and the
// keyboard flickered.
const OField=({label,value,onChange,placeholder,keyboard='default',note}:any)=>(
  <View style={{paddingHorizontal:16,paddingVertical:12,borderBottomWidth:1,borderBottomColor:C.hair}}>
    <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between'}}>
      <Text style={{color:C.text,fontSize:15}}>{label}</Text>
      <KInput style={{color:C.text,fontSize:15,textAlign:'right',flex:1,marginLeft:16,paddingVertical:6}} value={value} onChange={onChange} placeholder={placeholder} placeholderTextColor={C.muted} keyboardType={keyboard} autoCapitalize="none" autoCorrect={false} returnKeyType="done"/>
    </View>
    {note&&<Text style={{color:C.muted,fontSize:11,marginTop:4}}>{note}</Text>}
  </View>
);

export default function App() {
  // KEYBOARD_CALM_V1: background polling pauses while the keyboard is up.
  const kbOpenRef=useRef(false);
  useEffect(()=>{
    const a=Keyboard.addListener('keyboardWillShow',()=>{kbOpenRef.current=true;});
    const b=Keyboard.addListener('keyboardDidHide',()=>{kbOpenRef.current=false;});
    return()=>{a.remove();b.remove();};
  },[]);

  // ── Boot state ──
  const [booting,setBooting]           = useState(true);
  const [showOnboarding,setOnboarding] = useState(false);
  const [demoMode,setDemoMode]         = useState(false);

  // ── Connection ──
  const [initialized,setInitialized]   = useState(false);
  const [initError,setInitError]       = useState<string|null>(null);
  const [jetsonOk,setJetsonOk]         = useState<boolean|null>(null);
  const [serverVersion,setServerVersion]= useState<string|null>(null);
  const [refreshing,setRefreshing]     = useState(false);

  // ── Sync ──
  const [syncing,setSyncing]           = useState(false);
  const [lastSync,setLastSync]         = useState<string|null>(null);
  const [countdown,setCountdown]       = useState(AUTO_SYNC_SEC);
  const [lastIns,setLastIns]           = useState<number|null>(null);
  const [lastSkip,setLastSkip]         = useState<number|null>(null);

  // ── Data ──
  const [today,setToday]               = useState<DailySummary|null>(null);
  const [history,setHistory]           = useState<DailySummary[]>([]);
  const [alerts,setAlerts]             = useState<AlertEntry[]>([]);
  const [cameraStatus,setCameraStatus] = useState<CameraStatus|null>(null);
  const [cameraFrame,setCameraFrame]   = useState<string|null>(null);
  const [rooms,setRooms]               = useState<Room[]>([]);
  const [activeRoomId,setActiveRoomId] = useState<string>('room_1');
  const [persons,setPersons]                 = useState<Person[]>([]);
  const [personAlerts,setPersonAlerts]       = useState<PersonAlert[]>([]);
  const [showPersonModal,setShowPersonModal] = useState(false);
  const [pendingPersonAlert,setPendingPersonAlert] = useState<PersonAlert|null>(null);
  const [namingPersonId,setNamingPersonId]   = useState<string|null>(null);

  // Demo Mode: populate every screen from canned data and stop all polling.
  // No request is issued at all, so there is nothing to time out or fail.
  const enterDemo = useCallback(()=>{
    setDemoMode(true);
    setToday(DEMO_TODAY);
    setHistory(DEMO_HISTORY);
    setAlerts(DEMO_ALERTS);
    setPersons(DEMO_PERSONS);
    setCameraStatus(DEMO_CAMERA);
    setRooms(DEMO_ROOMS);
    setPersonAlerts([]);
    setCameraFrame(null);
  },[]);

  const exitDemo = useCallback(()=>{
    setDemoMode(false);
    setToday(null); setHistory([]); setAlerts([]);
    setPersons([]); setCameraStatus(null); setRooms([]);
  },[]);

  // Fold a duplicate detection into someone already known. Identity
  // fragments when a person is briefly seen, seen at an unusual distance,
  // or partly occluded; the caregiver can resolve it in one tap.
  const mergePerson = useCallback(async(sourceId:string,targetId:string,alertId?:number)=>{
    const u = urlRef.current||FALLBACK_URL;
    try{
      await AX.post(`${u}/persons/${sourceId}/merge`,
        {target_person_id:targetId},{timeout:8000});
      if(alertId!=null){
        try{ await AX.post(`${u}/persons/alerts/${alertId}/resolve`,{},{timeout:6000}); }
        catch(_e){}
      }
      setShowPersonModal(false); setPendingPersonAlert(null); setNamingPersonId(null);
      fetchPersons();
    }catch(e:any){
      log('ERROR',`Merge failed: ${e?.message??String(e)}`);
    }
    // fetchPersons and log are declared further down the component. Naming
    // them here would read them in the temporal dead zone. Both are stable
    // useCallback values, so the body resolves them correctly at call time.
  },[]);   // eslint-disable-line react-hooks/exhaustive-deps
  const [newPersonName,setNewPersonName]     = useState('');
  const nameRef=useRef('');
  const [isResidentFlag,setIsResidentFlag]   = useState(false);
  const [selectedPerson,setSelectedPerson]   = useState<Person|null>(null);
  const [zoneEditor,setZoneEditor]=useState(false);
  // IN_VIEW_LIVE_V1: exactly who is in the camera picture right now.
  const [inViewList,setInViewList]=useState<{id:string;name:string|null;is_resident:number}[]>([]);
  const [identifying,setIdentifying]=useState(0);
  // IDENTITY_V6_APP
  const [inViewOthers,setInViewOthers]=useState<{label:string;track:number|null}[]>([]);
  const [idMode,setIdMode]=useState<'resident'|'family'|null>(null);
  const loadIdMode=useCallback(async()=>{
    try{ const r=await AX.get(`${urlRef.current||FALLBACK_URL}/identity/mode`,{timeout:5000}); setIdMode(r.data?.mode==='resident'?'resident':'family'); }catch(_e){}
  },[]);
  const saveIdMode=useCallback(async(mode:'resident'|'family',residentId?:string)=>{
    try{
      await AX.post(`${urlRef.current||FALLBACK_URL}/identity/mode`,{mode,resident_person_id:residentId},{timeout:6000});
      setIdMode(mode);
    }catch(_e){ Alert.alert('Could not change the setting','Check the connection to the camera unit.'); }
  },[]);
  const [fullscreen,setFullscreen]     = useState(false);
  const [expandedAlert,setExpandedAlert]= useState<number|null>(null);
  const [replayFrames,setReplayFrames]  = useState<ReplayFrame[]|null>(null);
  const [replayMsg,setReplayMsg]        = useState<string|null>(null);

  // ── Settings ──
  const [residentName,setResidentName]   = useState('');
  const [residentHeight,setResidentHeight]= useState('170');
  const [emergencyPhone,setEmergencyPhone]= useState('');
  const [responderName,setResponderName]  = useState('');
  const [trendView,setTrendView]          = useState<'week'|'baseline'>('week');
  useEffect(()=>{AsyncStorage.getItem('vigil_caregiver_name')
    .then(v=>{if(v)setResponderName(v);}).catch(()=>{});},[]);
  const [emergencyEmail,setEmergencyEmail]= useState('');
  const [watchEnabled,setWatchEnabled]   = useState(false);
  const [serverIp,setServerIp]           = useState(DEFAULT_IP);
  const [serverPort,setServerPort]       = useState(DEFAULT_PORT);
  const [advancedOpen,setAdvancedOpen]   = useState(false);
  const [savingProfile,setSavingProfile] = useState(false);

  // ── UI ──
  const [activeTab,setActiveTab]         = useState<MainTab>('today');
  const [settingsTab,setSettingsTab]     = useState<SettingTab>('resident');
  const [showLogs,setShowLogs]           = useState(false);
  const [debugLog,setDebugLog]           = useState<DebugEntry[]>([]);

  // ── Refs ──
  const syncInFlight  = useRef(false);
  const autoTimer     = useRef<ReturnType<typeof setInterval>|null>(null);
  const cntTimer      = useRef<ReturnType<typeof setInterval>|null>(null);
  const urlRef        = useRef(FALLBACK_URL);
  const prevFrame     = useRef<string|null>(null);
  // PRIVACY_VIEW_V1: show residents as stick figures by default; video only on request.
  const [privacyView,setPrivacyView] = useState(true);
  const privacyViewRef = useRef(true);
  useEffect(()=>{AsyncStorage.getItem('vigil_privacy_view').then(v=>{if(v==='0'){setPrivacyView(false);privacyViewRef.current=false;}}).catch(()=>{});},[]);
  const togglePrivacyView=useCallback(()=>{
    const next=!privacyViewRef.current; privacyViewRef.current=next; setPrivacyView(next);
    prevFrame.current=null; setCameraFrame(null);
    AsyncStorage.setItem('vigil_privacy_view',next?'1':'0').catch(()=>{});
  },[]);
  const appStateRef   = useRef<AppStateStatus>('active');

  const alertCount = alerts.filter(a=>!a.seen).length;
  const url = ()=>urlRef.current||FALLBACK_URL;

  // ── Logging ──
  const log = useCallback((level:DebugEntry['level'],msg:string)=>{
    if (__DEV__) console.log(`[VIGIL][${level}] ${msg}`);
    setDebugLog(p=>[{ts:new Date().toLocaleTimeString(),level,msg},...p].slice(0,150));
  },[]);

  // ─────────────────────────────────────────────
  // BOOT — check onboarding and load saved settings
  // ─────────────────────────────────────────────
  useEffect(()=>{
    (async()=>{
      try{
        const [done,profileStr,serverStr]=await Promise.all([
          AsyncStorage.getItem(SK_ONBOARDING),
          AsyncStorage.getItem(SK_PROFILE),
          AsyncStorage.getItem(SK_SERVER),
        ]);
        if(profileStr){
          const p:SavedProfile=JSON.parse(profileStr);
          setResidentName(p.residentName||'');
          setResidentHeight(p.residentHeight||'170');
          setEmergencyPhone(p.emergencyPhone||'');
          setEmergencyEmail(p.emergencyEmail||'');
          setWatchEnabled(p.watchEnabled||false);
        }
        if(serverStr){
          const sv=JSON.parse(serverStr);
          const ip=sv.ip||DEFAULT_IP; const port=sv.port||DEFAULT_PORT;
          setServerIp(ip);setServerPort(port);
          const u=`http://${ip}:${port}`;urlRef.current=u;
        }
        if(done!=='done'){setOnboarding(true);}
      }catch{setOnboarding(true);}
      finally{setBooting(false);}
    })();
  },[]);

  // ─────────────────────────────────────────────
  // NETWORK
  // ─────────────────────────────────────────────
  const pingJetson=useCallback(async()=>{
    const u=url();
    try{
      const r=await AX.get(`${u}/health`);
      if(r.data?.status==='ok'){
        setJetsonOk(true);setServerVersion(r.data?.version??null);
      }else{setJetsonOk(false);}
    }catch{setJetsonOk(false);}
  },[]);

  const fetchDashboard=useCallback(async()=>{
    try{
      const [t,h]=await Promise.all([AX.get(`${url()}/summary/today`),AX.get(`${url()}/summary/recent`)]);
      if(t.data?.date)setToday(t.data);
      if(Array.isArray(h.data))setHistory(h.data);
    }catch(e:any){log('ERROR',`Dashboard: ${e?.message}`);}
  },[log]);

  const fetchAlerts=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/alerts/all`);
      if(Array.isArray(r.data))setAlerts(r.data);
    }catch(_){}
  },[]);

  const fetchCameraStatus=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/camera/status`,{timeout:5000});
      if(r.data)setCameraStatus(r.data);
    }catch{setCameraStatus({online:false,fps:null,person_present:false,last_seen_minutes_ago:null,camera_mode:null});}
  },[]);

  const fetchCameraSnapshot=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/camera/snapshot${privacyViewRef.current?'?view=skeleton&w=800':'?w=800'}`,{timeout:4000});
      if(r.data?.available&&r.data?.frame){
        // Only update if frame actually changed to reduce flicker
        if(r.data.frame!==prevFrame.current){
          prevFrame.current=r.data.frame;
          setCameraFrame(r.data.frame);
        }
      }
    }catch(_){}
  },[]);

  const fetchRooms=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/rooms`,{timeout:5000});
      if(Array.isArray(r.data))setRooms(r.data);
    }catch(_){}
  },[]);

  const fetchPersons=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/persons`,{timeout:5000});
      if(Array.isArray(r.data))setPersons(r.data);
    }catch(_){}
  },[]);

  // Refresh data when app comes back to foreground after being backgrounded.
  // Placed after pingJetson/fetchDashboard/fetchAlerts/fetchCameraStatus/fetchPersons
  // so all five dependencies are already declared (TS2448/TS2454 fix).
  useEffect(()=>{
    const sub = AppState.addEventListener('change', (next: AppStateStatus)=>{
      const wasBackground = appStateRef.current !== 'active';
      appStateRef.current = next;
      if(next === 'active' && wasBackground && jetsonOk === true){
        pingJetson();
        fetchDashboard();
        fetchAlerts();
        fetchPersons();
        fetchCameraStatus();
      }
    });
    return ()=> sub.remove();
  },[jetsonOk, pingJetson, fetchDashboard, fetchAlerts, fetchPersons, fetchCameraStatus]);

  // "Not now" hides a prompt for 30 minutes; without this the 6 s poll
  // reopened the sheet immediately after it was dismissed.
  const snoozedRef = useRef<{[id:number]:number}>({});
  const fetchPersonAlerts=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/persons/alerts`,{timeout:5000});
      const now=Date.now();
      const list:PersonAlert[]=Array.isArray(r.data)
        ?r.data.filter((a:PersonAlert)=>!((snoozedRef.current[a.id]??0)>now)):[];
      setPersonAlerts(list);
      if(list.length>0){
        setPendingPersonAlert(prev=>{
          if(!showPersonModal && !prev){
            setShowPersonModal(true);
            return list[list.length-1];   // server sends newest first; ask about the oldest
          }
          return prev;
        });
      }
    }catch(_){}
  },[showPersonModal]);

  // PEOPLE_UI_V1: edit someone who is already named.
  const savePersonEdit=useCallback(async(p:Person,name:string,isRes:boolean)=>{
    try{
      await AX.post(`${url()}/persons/${p.id}/identify`,{name,is_resident:isRes},{timeout:8000});
    }catch(e:any){log('ERROR',`Edit person: ${e?.message}`);}
    fetchPersons();
  },[fetchPersons,log]);
  const editPerson=useCallback((p:Person)=>{
    const others=persons.filter(o=>o.id!==p.id&&!!o.name);
    Alert.alert(p.name??'Person','What would you like to change?',[
      {text:'Rename',onPress:()=>Alert.prompt('Rename',`New name for ${p.name}`,[
        {text:'Cancel',style:'cancel'},
        {text:'Save',onPress:(v?:string)=>{const n=(v||'').trim();if(n)savePersonEdit(p,n,!!p.is_resident);}},
      ],'plain-text',p.name??'')},
      {text:p.is_resident?'Mark as visitor':'Mark as resident',onPress:()=>savePersonEdit(p,p.name??'Person',!p.is_resident)},
      ...(others.length>0?[{text:'Same person as another entry',onPress:()=>Alert.alert(
        `Merge ${p.name}`,'Everything recorded for this entry moves to the person you choose, and this entry is removed.',
        [...others.slice(0,6).map(o=>({text:`Merge into ${o.name}`,style:'destructive' as const,onPress:()=>mergePerson(p.id,o.id)})),
         {text:'Cancel',style:'cancel' as const}])}]:[]),
      {text:'Cancel',style:'cancel'},
    ]);
  },[persons,savePersonEdit,mergePerson]);

  const identifyPerson=useCallback(async(personId:string,name:string,isRes:boolean,alertId?:number)=>{
    try{
      await AX.post(`${url()}/persons/${personId}/identify`,{name,is_resident:isRes});
      if(alertId){await AX.post(`${url()}/persons/alerts/${alertId}/resolve`,{});}
    }catch(e:any){log('ERROR',`Identify: ${e?.message}`);}
    setShowPersonModal(false);
    setPendingPersonAlert(null);
    setNamingPersonId(null);
    setNewPersonName('');
    await fetchPersons();
    await fetchPersonAlerts();
  },[fetchPersons,fetchPersonAlerts,log]);

  const acknowledgeAlert=useCallback(async(id:number)=>{
    // Only mark it seen if the server confirms. The previous version
    // swallowed every error and marked it acknowledged regardless, so the
    // button appeared to work for two months while nothing was recorded.
    if(demoMode){
      // Demo Mode has no server. Update locally so the button works for a
      // reviewer instead of silently failing.
      setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1,acknowledged_by:'Demo caregiver'} as any:a));
      return;
    }
    try{
      const who = await caregiverName();
      await AX.post(`${url()}/alerts/${id}/ack`,{acknowledged_by:who});
      setAlerts(p=>p.map(a=>a.id===id?{...a,seen:1,acknowledged_by:who} as any:a));
    }catch(e:any){
      log('ERROR',`Could not acknowledge alert ${id}: ${e?.message??String(e)}`);
    }
  },[log,demoMode]);

  const loadConfig=useCallback(async()=>{
    try{
      const r=await AX.get(`${url()}/config`);
      if(r.data){
        if(r.data.resident_name)setResidentName(r.data.resident_name);
        if(r.data.emergency_phone)setEmergencyPhone(r.data.emergency_phone);
        if(r.data.emergency_email)setEmergencyEmail(r.data.emergency_email);
        if(r.data.resident_height_cm)setResidentHeight(String(r.data.resident_height_cm));
      }
    }catch(_){}
  },[]);

  // ─────────────────────────────────────────────
  // HEALTHKIT INIT
  // ─────────────────────────────────────────────
  // PEOPLE_UI_V1: HealthKit start is a function so the status pill can retry it.
  const startHealthKit=useCallback(()=>{
    setInitError(null);
    if(!AppleHealthKit){
      const m=`HealthKit is not available in this build (${HK_LOAD_ERROR??'unknown load failure'}).`;
      log('WARN',m);setInitError(m);return;
    }
    const HKP=AppleHealthKit?.Constants?.Permissions;
    if(!HKP||typeof AppleHealthKit.initHealthKit!=='function'){
      const m='The HealthKit native module is not linked. Run pod install in the ios folder, then Clean Build Folder in Xcode.';
      log('ERROR',m);setInitError(m);return;
    }
    const perms={permissions:{read:[
      HKP.StepCount,
      HKP.RestingHeartRate,
      HKP.HeartRateVariability,
      HKP.HeartRate,
      HKP.SleepAnalysis,
      HKP.ActiveEnergyBurned,
      HKP.DistanceWalkingRunning,
      HKP.OxygenSaturation,
      HKP.RespiratoryRate,
    ],write:[] as string[]}};
    log('INFO','Initializing HealthKit...');
    AppleHealthKit.initHealthKit(perms,(err:any)=>{
      if(err){
        const m=typeof err==='string'?err:(err?.message??JSON.stringify(err));
        log('ERROR',`HK: ${m}`);setInitError(m);return;
      }
      log('OK','HealthKit ready');setInitError(null);setInitialized(true);
    });
  },[log]);
  useEffect(()=>{startHealthKit();},[startHealthKit]);

  // ─────────────────────────────────────────────
  // SERVER POLLING EFFECTS
  // ─────────────────────────────────────────────
  useEffect(()=>{if(!booting&&!showOnboarding&&!demoMode)setTimeout(()=>pingJetson(),1500);},[booting,showOnboarding,demoMode,pingJetson]);

  useEffect(()=>{
    if(!initialized||jetsonOk!==true||demoMode)return;
    fetchDashboard();fetchAlerts();loadConfig();fetchRooms();
    setCountdown(AUTO_SYNC_SEC);
    // The per-second countdown re-rendered the whole app every second; removed.
    autoTimer.current=setInterval(()=>{if(!kbOpenRef.current)performSync(true);},AUTO_SYNC_SEC*1000);
    performSync(true);
    return()=>{if(autoTimer.current)clearInterval(autoTimer.current);if(cntTimer.current)clearInterval(cntTimer.current);};
  },[initialized,jetsonOk]); // eslint-disable-line

  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchCameraStatus();const id=setInterval(()=>{if(!kbOpenRef.current)fetchCameraStatus();},10000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchCameraStatus]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal||activeTab!=='today')return;let alive=true;(async()=>{while(alive){try{if(!kbOpenRef.current)await fetchCameraSnapshot();}catch(_e){}await new Promise(r=>setTimeout(r,100));}})();return()=>{alive=false;};},[jetsonOk,showPersonModal,activeTab,fetchCameraSnapshot]);
  // Camera-derived metrics (gait speed / cadence / posture) refresh every 4s so they
  // visibly move while someone is walking in frame — the 60s HealthKit auto-sync
  // (above) is far too slow for this to feel live during a walk-test or demo.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchDashboard();const id=setInterval(()=>{if(!kbOpenRef.current)fetchDashboard();},4000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchDashboard]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(()=>{if(!kbOpenRef.current)fetchRooms();},15000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchRooms]);
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersons();const id=setInterval(()=>{if(!kbOpenRef.current)fetchPersons();},8000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersons]);
  // Tightened from 30s -> 6s: this is the "more than one person detected" identification
  // sheet — for a live demo this needs to feel near-instant when a second person walks in.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;fetchPersonAlerts();const id=setInterval(()=>{if(!kbOpenRef.current)fetchPersonAlerts();},6000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchPersonAlerts]);
  // Tightened from 15s -> 5s: fall/stillness alerts should reach the UI almost as fast
  // as the SMS itself arrives, not lag up to 15s behind it.
  useEffect(()=>{if(!jetsonOk||demoMode||showPersonModal)return;const id=setInterval(()=>{if(!kbOpenRef.current)fetchAlerts();},5000);return()=>clearInterval(id);},[jetsonOk,showPersonModal,fetchAlerts]);

  // ─────────────────────────────────────────────
  useEffect(()=>{
    if(!jetsonOk||demoMode||activeTab!=='today')return;
    let alive=true;
    const tick=async()=>{
      if(kbOpenRef.current)return;
      try{
        const r=await AX.get(`${urlRef.current||FALLBACK_URL}/persons/in-view`,{timeout:3000});
        if(alive){ setInViewList(Array.isArray(r.data?.people)?r.data.people:[]); setIdentifying(Number(r.data?.identifying??0)); setInViewOthers(Array.isArray(r.data?.others)?r.data.others:[]); }
      }catch(_e){}
    };
    tick(); const id=setInterval(tick,2000);
    return()=>{alive=false;clearInterval(id);};
  },[jetsonOk,demoMode,activeTab]);
  // HEALTHKIT SYNC
  // ─────────────────────────────────────────────
  const performSync=useCallback(async(silent=false)=>{
    if(syncInFlight.current||!initialized||jetsonOk!==true||demoMode)return;
    syncInFlight.current=true;setSyncing(true);
    const tr=todayRange();const rr=rollingRange(168);
    try{
      const[steps,cals,dist]=await Promise.all([
        readDailyTotal('getStepCount',tr,'StepCount','count',log),
        readDailyTotal('getActiveEnergyBurned',tr,'ActiveEnergyBurned','kcal',log),
        readDailyTotal('getDistanceWalkingRunning',tr,'DistanceWalkingRunning','m',log),
      ]);
      const[rhr,hrv,spo2,resp,sleep]=await Promise.all([
        readSamples('getRestingHeartRate',tr,'RestingHeartRate','bpm',log),
        readSamples('getHeartRateVariabilitySamples',tr,'HeartRateVariabilitySDNN','ms',log),
        readSamples('getOxygenSaturationSamples',rr,'OxygenSaturation','ratio',log),
        readSamples('getRespiratoryRateSamples',rr,'RespiratoryRate','brpm',log),
        readSamples('getSleepSamples',rr,'SleepAnalysis','hr',log),
      ]);
      const spo2Pct=spo2.map(s=>({...s,value:s.value<=1?s.value*100:s.value,unit:'%'}));
      const samples=[...steps,...cals,...dist,...rhr,...hrv,...spo2Pct,...resp,...sleep];
      if(!samples.length){if(!silent)log('WARN','No samples');return;}
      const r=await AX.post(`${url()}/sync/healthkit`,{samples});
      setLastIns(r.data?.inserted??0);setLastSkip(r.data?.skipped??0);
      setLastSync(new Date().toLocaleTimeString());
      log('OK',`Sync: +${r.data?.inserted??0}`);
      await fetchDashboard();
    }catch(e:any){log('ERROR',`Sync: ${e?.message}`);if(!silent)Alert.alert('Sync Failed',e?.message);}
    finally{setSyncing(false);syncInFlight.current=false;}
  },[initialized,jetsonOk,log,fetchDashboard]);

  // ─────────────────────────────────────────────
  // SAVE PROFILE
  // ─────────────────────────────────────────────
  const saveProfile=useCallback(async()=>{
    setSavingProfile(true);
    try{
      await AX.post(`${url()}/config`,{
        resident_name:residentName||null,
        emergency_phone:emergencyPhone||null,
        emergency_email:emergencyEmail||null,
        resident_height_cm:parseFloat(residentHeight)||170,
      });
      const profile:SavedProfile={residentName,residentHeight,emergencyPhone,emergencyEmail,watchEnabled};
      await AsyncStorage.setItem(SK_PROFILE,JSON.stringify(profile));
      Alert.alert('Saved','Profile saved and synced to your VIGIL server.');
    }catch(e:any){Alert.alert('Save Failed',`Could not reach server: ${e?.message}`);}
    finally{setSavingProfile(false);}
  },[residentName,emergencyPhone,emergencyEmail,residentHeight,watchEnabled]);

  const reconnect=()=>{
    const u=`http://${serverIp}:${serverPort}`;
    urlRef.current=u;setJetsonOk(null);
    AsyncStorage.setItem(SK_SERVER,JSON.stringify({ip:serverIp,port:serverPort}));
    setTimeout(()=>pingJetson(),300);
  };

  const onRefresh=async()=>{setRefreshing(true);await pingJetson();await fetchDashboard();await fetchAlerts();setRefreshing(false);};

  // ─────────────────────────────────────────────
  // CAMERA FEED COMPONENT
  // ─────────────────────────────────────────────
  const camBorder=cameraStatus?.online?C.green+'55':C.border;

  // Drawn in place, not as a component: an inner component is rebuilt on
  // every render, which reloaded the picture and made the feed stutter.
  const renderCameraFeed=(fullSize:boolean=false)=>{
    const w=fullSize?SW-32:CAM_W;
    const h=fullSize?Math.round((SW-32)*9/16):CAM_H;
    return(
      <View style={jetsonOk===true&&cameraFrame?{borderRadius:20,shadowColor:C.green,shadowOpacity:0.15,shadowRadius:16,shadowOffset:{width:0,height:0}}:undefined}>
      <View style={{width:w,height:h,borderRadius:20,overflow:'hidden',borderWidth:1.5,borderColor:camBorder,backgroundColor:C.card}}>
        {cameraFrame?(
          <>
            <Image
              source={{uri:`data:image/jpeg;base64,${cameraFrame}`}}
              style={{width:w,height:h}} resizeMode="cover"
              fadeDuration={0}/>
            <TouchableOpacity
              style={{position:'absolute',top:10,right:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:6,flexDirection:'row',alignItems:'center',paddingHorizontal:8,paddingVertical:4,gap:5}}
              onPress={()=>setFullscreen(true)}>
              <Animated.View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,transform:[{scale:LIVE_PULSE}]}}/>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'700',letterSpacing:1}}>LIVE</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={{position:'absolute',bottom:10,right:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:8,paddingHorizontal:10,paddingVertical:5}}
              onPress={()=>setFullscreen(true)}>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'600'}}>Full Screen</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={{position:'absolute',top:10,left:10,backgroundColor:'rgba(0,0,0,0.65)',borderRadius:8,paddingHorizontal:10,paddingVertical:5}}
              onPress={togglePrivacyView}>
              <Text style={{color:'#fff',fontSize:11,fontWeight:'600'}}>{privacyView?'Stick figures (tap for video)':'Video (tap for stick figures)'}</Text>
            </TouchableOpacity>
          </>
        ):(
          <View style={{flex:1,alignItems:'center',justifyContent:'center',gap:6}}>
            <Text style={{color:C.muted,fontSize:14}}>
              {demoMode?'Live View \u2014 Demo':'Live View Unavailable'}
            </Text>
            <Text style={{color:C.muted,fontSize:11,textAlign:'center',paddingHorizontal:28,lineHeight:16}}>
              {demoMode
                ?'Live video needs the VIGIL camera unit. Everything else on this screen is sample data.'
                :(cameraStatus?.online?'Camera starting...':'Camera offline')}
            </Text>
          </View>
        )}
      </View>
      </View>
    );
  };

  // ─────────────────────────────────────────────
  // TODAY SCREEN
  // ─────────────────────────────────────────────
  const namedPeople=persons.filter(p=>!!p.name);
  const residentPerson=namedPeople.find(p=>!!p.is_resident)??null;
  const renderToday=()=>(
    <ScrollView refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.green}/>} showsVerticalScrollIndicator={false}>
      <View style={s.pageHead}>
        <View style={{flexDirection:'row',alignItems:'center',gap:10,marginBottom:16}}>
          <Image source={{uri:'LaunchLogo'}} style={{width:30,height:30,borderRadius:8}}/>
          <Text style={{color:C.text,fontSize:15,fontWeight:'800',letterSpacing:2.4}}>VIGIL</Text>
        </View>
        <Text style={s.pageTitle}>Today</Text>
        <Text style={s.pageSub}>{new Date().toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric'})}</Text>
      </View>

      {/* Status pills */}
      <View style={s.pillRow}>
        <TouchableOpacity activeOpacity={initError?0.6:1} style={s.pill}
          onPress={()=>{if(initError)Alert.alert('Apple Health is not connected',
            `${initError}\n\nVIGIL's camera monitoring does not depend on it. Apple Health only adds Apple Watch heart rate and sleep.`,
            [{text:'Try again',onPress:()=>startHealthKit()},{text:'OK',style:'cancel'}]);}}>
          <View style={[s.pillDot,{backgroundColor:initialized?C.green:initError?C.orange:C.sub}]}/><Text style={s.pillTxt}>{initialized?'Apple Health':initError?'Health off':'Starting'}</Text></TouchableOpacity>
        <View style={s.pill}><View style={[s.pillDot,{backgroundColor:demoMode||jetsonOk===true?C.green:jetsonOk===false?C.red:C.sub}]}/><Text style={s.pillTxt}>{demoMode?'Demo unit':jetsonOk===true?'Monitoring unit':jetsonOk===false?'Unit offline':'Checking'}</Text></View>
        {jetsonOk===true&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:syncing?C.orange:C.green}]}/><Text style={s.pillTxt}>{syncing?'Syncing':'Synced'}</Text></View>}
        {cameraStatus?.online&&<View style={s.pill}><View style={[s.pillDot,{backgroundColor:C.green}]}/><Text style={s.pillTxt}>{String(cameraStatus.fps?.toFixed(0)??'?')} fps</Text></View>}
      </View>

      {(()=>{
        // An unacknowledged fall is the first thing on screen, not a list
        // item one tap away.
        const open = alerts.filter(a=>a.alert_type==='fall'&&!a.seen);
        if(open.length===0) return null;
        const a = open[0];
        return (
          <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.red+'1F',
                        borderRadius:16,padding:16,borderWidth:1,borderColor:C.red+'66'}}>
            <Text style={{color:C.red,fontSize:12,fontWeight:'800',letterSpacing:0.6}}>
              FALL NOT YET ACKNOWLEDGED
            </Text>
            <Text style={{color:C.text,fontSize:14,marginTop:6,lineHeight:20}}>
              {`Detected ${timeAgo(a.detected_at)}. Someone should check on the resident and confirm here.`}
            </Text>
            <TouchableOpacity onPress={()=>acknowledgeAlert(a.id)}
              style={{backgroundColor:C.red,borderRadius:12,paddingVertical:13,
                      alignItems:'center',marginTop:12}}>
              <Text style={{color:'#fff',fontWeight:'800',fontSize:15}}>
                {open.length>1?`I am checking on them (${open.length} open)`:'I am checking on them'}
              </Text>
            </TouchableOpacity>
          </View>
        );
      })()}

      {/* Demo entry: the only path an App Store reviewer has, since they
          have no Jetson, no camera and no VPN. */}
      {!demoMode&&jetsonOk===false&&(
        <View style={{marginHorizontal:16,marginBottom:12,backgroundColor:C.card,
                      borderRadius:16,padding:16,borderWidth:1,borderColor:C.border}}>
          <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>No monitoring unit connected</Text>
          <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginTop:6}}>
            VIGIL needs its camera unit on the same network. To see how the app
            works without hardware, open Demo Mode.
          </Text>
          <TouchableOpacity onPress={enterDemo}
            style={{backgroundColor:C.green,borderRadius:12,paddingVertical:12,
                    alignItems:'center',marginTop:14}}>
            <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Open Demo Mode</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Room switcher */}
      {rooms.length>1&&(
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{paddingHorizontal:16,paddingBottom:8,gap:8,flexDirection:'row'}}>
          {rooms.map(r=>(
            <TouchableOpacity key={r.id} onPress={()=>setActiveRoomId(r.id)}
              style={{paddingHorizontal:14,paddingVertical:7,borderRadius:20,backgroundColor:activeRoomId===r.id?C.green:C.card,flexDirection:'row',alignItems:'center',gap:6}}>
              <View style={{width:6,height:6,borderRadius:3,backgroundColor:r.camera_online?(activeRoomId===r.id?'#000':C.green):C.muted}}/>
              <Text style={{color:activeRoomId===r.id?'#000':C.text,fontSize:13,fontWeight:'600'}}>{r.name}</Text>
              {(r.person_count??0)>1&&<Text style={{color:activeRoomId===r.id?'#000':C.orange,fontSize:11,fontWeight:'700'}}>{String(r.person_count)}</Text>}
            </TouchableOpacity>
          ))}
        </ScrollView>
      )}

      {/* Camera status */}
      <View style={[s.card,{marginHorizontal:16,marginTop:12,marginBottom:4,borderWidth:1,borderColor:camBorder}]}>
        <View style={{flexDirection:'row',alignItems:'center',gap:10}}>
          <View style={{width:12,height:12,borderRadius:6,backgroundColor:cameraStatus?.online?C.green:C.red}}/>
          <View style={{flex:1}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'700'}}>
              {cameraStatus?.online&&cameraStatus?.person_present?'Resident Detected':cameraStatus?.online?'Room Empty':'Camera Offline'}
            </Text>
            <Text style={{color:C.sub,fontSize:12,marginTop:2}}>
              {cameraStatus?.online?`${cameraStatus.fps?.toFixed(1)??'?'} fps · Live`:'Monitoring unit offline'}
            </Text>
          </View>
          {(cameraStatus?.person_count??0)>1&&(
            <View style={{backgroundColor:C.orange+'22',borderRadius:8,paddingHorizontal:8,paddingVertical:3}}>
              <Text style={{color:C.orange,fontSize:11,fontWeight:'700'}}>{String(cameraStatus?.person_count)} people</Text>
            </View>
          )}
        </View>
      </View>

      {/* Live camera feed */}
      <Text style={s.secHeader}>Camera Feed</Text>
      <View style={{marginHorizontal:16,marginBottom:4}}>{renderCameraFeed()}</View>
      {!demoMode&&(()=>{
        // IN_VIEW_V1: who the camera recognises right now.
        const inView=inViewList.map(v=>persons.find(p=>p.id===v.id)??({id:v.id,name:v.name,is_resident:v.is_resident,room_id:'',height_px:null,confidence:0,frames_seen:0,first_seen_at:null,last_seen_at:null} as Person));
        return(
          <View style={{marginHorizontal:16,marginTop:12}}>
            <Text style={{color:C.sub,fontSize:13,fontWeight:'700',marginBottom:8}}>In view now <Text style={{color:C.muted,fontWeight:'500'}}>(tap a name to correct it)</Text></Text>
            {inView.length===0&&identifying===0
              ?<Text style={{color:C.muted,fontSize:14}}>No one in view.</Text>
              :<View style={{flexDirection:'row',flexWrap:'wrap',gap:8}}>
                {inView.map(p=>(
                  <TouchableOpacity key={p.id} activeOpacity={0.7}
                    onPress={()=>{
                      // CORRECT_PERSON_V1: fix a wrong name on the spot.
                      const others=persons.filter(o=>!!o.name&&o.id!==p.id);
                      if(!p.name){
                        if(others.length===0)return;
                        Alert.alert('Who is this?','VIGIL will remember this look for them.',[
                          ...others.slice(0,6).map(o=>({text:`Same as ${o.name}`,onPress:()=>mergePerson(p.id,o.id)})),
                          {text:'Cancel',style:'cancel' as const},
                        ]);
                        return;
                      }
                      Alert.alert(`Is this ${p.name}?`,'Confirm to help VIGIL remember this look, or choose who it really is.',[
                        {text:`Yes, this is ${p.name}`,onPress:()=>{
                          AX.post(`${urlRef.current||FALLBACK_URL}/persons/${p.id}/confirm`,{},{timeout:8000}).catch(()=>{});
                        }},
                        ...others.slice(0,5).map(o=>({text:`No, this is ${o.name}`,onPress:()=>{
                          AX.post(`${urlRef.current||FALLBACK_URL}/persons/${p.id}/correct`,{target_person_id:o.id},{timeout:8000})
                            .then(()=>{setTimeout(()=>fetchPersons(),3500);})
                            .catch(()=>Alert.alert('Could not send the correction','Check the connection to the camera unit.'));
                        }})),
                        {text:'Cancel',style:'cancel' as const},
                      ]);
                    }}
                    style={{flexDirection:'row',alignItems:'center',gap:8,paddingLeft:5,paddingRight:14,paddingVertical:5,borderRadius:30,
                    backgroundColor:p.name?C.green+'18':C.card,borderWidth:1.5,borderColor:p.name?C.green+'66':C.border}}>
                    <PersonAvatar personId={p.id} name={p.name} serverUrl={urlRef.current} size={30} isResident={!!p.is_resident}/>
                    <Text style={{color:C.text,fontWeight:'700',fontSize:14}}>{p.name??'New person'}</Text>
                  </TouchableOpacity>
                ))}
                {(inViewOthers.length?inViewOthers:Array.from({length:identifying},()=>({label:'Identifying...',track:null}))).map((o,i)=>(
                  <TouchableOpacity key={'o'+i} activeOpacity={0.7}
                    onPress={()=>{
                      if(o.track==null)return;
                      const named=persons.filter(x=>!!x.name);
                      if(!named.length)return;
                      Alert.alert('Who is this?','VIGIL will name them now and remember this look.',[
                        ...named.slice(0,6).map(x=>({text:`This is ${x.name}`,onPress:()=>{
                          AX.post(`${urlRef.current||FALLBACK_URL}/tracks/${o.track}/assign`,{target_person_id:x.id},{timeout:8000})
                            .catch(()=>Alert.alert('Could not send','Check the connection to the camera unit.'));
                        }})),
                        {text:'Cancel',style:'cancel' as const},
                      ]);
                    }}
                    style={{flexDirection:'row',alignItems:'center',gap:8,paddingHorizontal:14,paddingVertical:10,borderRadius:30,backgroundColor:C.card,borderWidth:1.5,borderColor:o.label==='Visitor'?C.blue+'66':C.border}}>
                    {o.label==='Identifying...'&&<ActivityIndicator size="small" color={C.sub}/>}
                    <Text style={{color:o.label==='Visitor'?C.blue:C.sub,fontWeight:'700',fontSize:14}}>{o.label}</Text>
                  </TouchableOpacity>
                ))}
              </View>}
          </View>
        );
      })()}

      {/* Activity ring -- HOME_RESIDENT_APP: the Today page is one person, the resident */}
      <Text style={s.secHeader}>{residentName?`${residentName}'s activity`:'Activity'}</Text>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:20,marginTop:-6,lineHeight:17}}>Share of the time {residentName||'the resident'} was in view today that they were moving. Time out of view doesn't count.</Text>
      <ActivityRing pct={((today?.active_minutes??0)+(today?.still_minutes??0))>=20?(today?.activity_level_pct??null):null} activeMin={today?.active_minutes??null} stillMin={today?.still_minutes??null}/>

      {/* Camera metrics */}
      <Text style={s.secHeader}>{residentName?`${residentName}'s walking and posture`:'Walking and posture'}</Text>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:20,marginTop:-6,marginBottom:8,lineHeight:17}}>Today's averages for {residentName||'the resident'} only. Other people are on their own pages under Trends.</Text>
      <View style={s.grid}>
        {[
          {label:'Gait Speed',val:fmt(today?.camera_gait_speed_ms,2),unit:'m/s',color:C.green,warn:(today?.camera_gait_speed_ms??1)<0.8&&today?.camera_gait_speed_ms!=null},
          {label:'Posture',val:fmt(today?.posture_score,0),unit:'/100',color:C.purple,warn:(today?.posture_score??100)<50&&today?.posture_score!=null},
          {label:'Asymmetry',val:fmt(today?.camera_asymmetry_pct,1),unit:'%',color:C.orange,warn:(today?.camera_asymmetry_pct??0)>15},
          {label:'Cadence',val:fmt(today?.camera_cadence_spm,0),unit:'spm',color:C.blue,warn:false},
          {label:'Longest Still',val:fmt(today?.longest_stillness_min,0),unit:'min',color:C.orange,warn:(today?.longest_stillness_min??0)>90},
          {label:'Data Points',val:fmtLarge(today?.camera_data_points),unit:'pts',color:C.sub,warn:false},
        ].map(m=>(
          <View key={m.label} style={s.metCard}>
            <View style={s.metTop}>
              <View style={[s.metDot,{backgroundColor:m.warn?C.red:m.color}]}/>
              <Text style={s.metLbl}>{m.label}</Text>
            </View>
            <Text style={[s.metVal,{color:m.warn?C.red:C.text}]}>{m.val}</Text>
            <Text style={s.metUnit}>{m.unit}</Text>
            {m.warn&&<View style={s.warnChip}><Text style={s.warnChipTxt}>OUTSIDE RANGE</Text></View>}
          </View>
        ))}
      </View>

      {/* Apple Watch — only if enabled and data exists */}
      {watchEnabled&&(today?.hrv_sdnn!=null||today?.resting_hr!=null)&&(
        <>
          <Text style={s.secHeader}>Apple Watch</Text>
          <View style={s.grid}>
            {[
              {label:'Steps',val:fmtLarge(today?.step_count),unit:'steps',color:C.green,warn:false},
              {label:'Resting HR',val:fmt(today?.resting_hr,0),unit:'bpm',color:C.blue,warn:false},
              {label:'HRV',val:fmt(today?.hrv_sdnn,1),unit:'ms',color:C.blue,warn:false},
              {label:'Calories',val:fmtLarge(today?.active_calories),unit:'kcal',color:C.orange,warn:false},
              {label:'SpO2',val:fmt(today?.spo2_avg,1),unit:'%',color:C.purple,warn:(today?.spo2_avg??100)<95&&today?.spo2_avg!=null},
              {label:'Sleep',val:fmt(today?.sleep_hours,1),unit:'hrs',color:C.orange,warn:false},
            ].map(m=>(
              <View key={m.label} style={[s.metCard,{borderTopColor:m.warn?C.red:m.color}]}>
                <View style={s.metTop}>
                  <View style={[s.metDot,{backgroundColor:m.warn?C.red:m.color}]}/>
                  <Text style={s.metLbl}>{m.label}</Text>
                </View>
                <Text style={[s.metVal,{color:m.warn?C.red:C.text}]}>{m.val}</Text>
                <Text style={s.metUnit}>{m.unit}</Text>
              </View>
            ))}
          </View>
        </>
      )}
      {lastSync&&<Text style={{color:C.muted,fontSize:11,textAlign:'center',marginTop:12}}>Last synced {lastSync}  ·  +{lastIns??0} new</Text>}
      <View style={{height:32}}/>
    </ScrollView>
  );

  // ─────────────────────────────────────────────
  // TRENDS SCREEN
  // ─────────────────────────────────────────────
  const renderTrends=()=>{
    const t2=new Date();
    const sun=new Date(t2);sun.setDate(t2.getDate()-t2.getDay());sun.setHours(0,0,0,0);
    const weekDates=Array.from({length:7},(_,i)=>{const d=new Date(sun);d.setDate(sun.getDate()+i);return d.toISOString().slice(0,10);});
    const todayStr=new Date().toISOString().slice(0,10);
    const byDate:Record<string,DailySummary>={};history.forEach(r=>{byDate[r.date]=r;});
    const week=(k:keyof DailySummary)=>weekDates.map(d=>{if(d>todayStr)return null;return(byDate[d]?.[k] as number|null)??null;});
    const DAY=['S','M','T','W','T','F','S'];

    const TrendBar=({data,label,unit,color,dec=0}:{data:(number|null)[];label:string;unit:string;color:string;dec?:number})=>{
      const valid=data.filter(v=>v!=null) as number[];
      if(!valid.length)return null;
      const mean=valid.reduce((a,b)=>a+b,0)/valid.length;
      const std=valid.length>1?Math.sqrt(valid.reduce((a,b)=>a+(b-mean)**2,0)/valid.length):0;
      const min=Math.min(...valid);const max=Math.max(...valid);const range=max-min||1;
      const latest=data.filter(v=>v!=null).slice(-1)[0];
      const BAR=48;const meanBot=Math.max(2,((mean-min)/range)*BAR);
      return(
        <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
          <View style={{flexDirection:'row',justifyContent:'space-between',marginBottom:8}}>
            <View>
              <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600',marginBottom:2}}>{label}</Text>
              <Text style={{color,fontSize:22,fontWeight:'800'}}>{latest!=null?latest.toFixed(dec):'—'}<Text style={{color:C.sub,fontSize:12}}> {unit}</Text></Text>
            </View>
            <Text style={{color:C.muted,fontSize:11}}>avg {mean.toFixed(dec)}</Text>
          </View>
          <View style={{height:BAR+8,position:'relative'}}>
            <View style={{position:'absolute',left:0,right:0,bottom:0,top:0,flexDirection:'row',alignItems:'flex-end',gap:3}}>
              {data.map((v,i)=>{
                const isFuture=weekDates[i]>todayStr;
                if(v==null||isFuture)return<View key={i} style={{flex:1,height:3,backgroundColor:C.muted+'20',borderRadius:2}}/>;
                const h=Math.max(4,((v-min)/range)*BAR);
                const isAnom=std>0.001&&Math.abs(v-mean)>1.5*std;
                return(
                  <View key={i} style={{flex:1,height:BAR+8,justifyContent:'flex-end',alignItems:'center'}}>
                    {isAnom?<Text style={{fontSize:9,color:C.red,fontWeight:'800',marginBottom:1}}>!</Text>:<View style={{height:11}}/>}
                    <View style={{width:'100%',height:h,backgroundColor:isAnom?C.red:color,borderRadius:3}}/>
                  </View>
                );
              })}
            </View>
            {valid.length>=2&&<View pointerEvents="none" style={{position:'absolute',left:0,right:0,bottom:meanBot,height:1,borderStyle:'dashed',borderWidth:1,borderColor:C.muted+'AA'}}/>}
          </View>
          <View style={{flexDirection:'row',marginTop:4,gap:3}}>
            {weekDates.map((d,i)=>{
              const isToday=d===todayStr;const isFuture=d>todayStr;
              return<Text key={i} style={{flex:1,textAlign:'center',fontSize:9,color:isToday?color:isFuture?C.muted+'40':C.muted,fontWeight:isToday?'700':'400'}}>{DAY[new Date(d+'T12:00:00').getDay()]}</Text>;
            })}
          </View>
        </View>
      );
    };

    // Baseline view: each measure against the resident's own usual range,
    // computed from the days BEFORE the latest week so the week being
    // judged is never part of its own standard.
    const BaselineCard=({k,label,unit,color,dec=0,higherWorse=false}:{k:keyof DailySummary;label:string;unit:string;color:string;dec?:number;higherWorse?:boolean})=>{
      const POS_ONLY=['camera_gait_speed_ms','camera_cadence_spm','posture_score','resting_hr','hrv_sdnn','spo2_avg','sleep_hours','stride_variability'];
      const num=(r:DailySummary):number|null=>{const v=r[k] as any;if(!(typeof v==='number'&&isFinite(v)))return null;if(v<=0&&POS_ONLY.includes(k as string))return null;return v;};
      const asc=[...history].sort((a,b)=>a.date<b.date?-1:a.date>b.date?1:0);
      const recent=asc.slice(-14).map(r=>({d:r.date,v:num(r)}));
      if(recent.every(x=>x.v==null))return null;
      const base=asc.slice(0,Math.max(0,asc.length-7)).slice(-30).map(num).filter((v):v is number=>v!=null);
      if(base.length<5){
        return(
          <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
            <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600'}}>{label}</Text>
            <Text style={{color:C.muted,fontSize:12,marginTop:6}}>
              {`Learning their usual range: ${base.length} of 5 earlier days recorded.`}
            </Text>
          </View>
        );
      }
      // Robust usual range: median +/- 1.5 x scaled median absolute deviation.
      // A few odd days (no walking seen, a bad reading) can no longer stretch
      // the band until nothing falls outside it.
      const srt=[...base].sort((a,b)=>a-b);
      const med=(a:number[])=>{const n=a.length;return n%2?a[(n-1)/2]:(a[n/2-1]+a[n/2])/2;};
      const mean=med(srt);
      const mad=med(srt.map(v=>Math.abs(v-mean)).sort((a,b)=>a-b));
      const std=Math.max(1.4826*mad,Math.abs(mean)*0.02);
      const lo=mean-1.5*std,hi=mean+1.5*std;
      const vals=recent.filter(x=>x.v!=null).map(x=>x.v as number);
      const top=Math.max(hi,...vals),bot=Math.min(lo,...vals);
      const span=(top-bot)||1;const H=70;
      const y=(v:number)=>((v-bot)/span)*H;
      const last=[...recent].reverse().find(x=>x.v!=null);
      const lv=last?(last.v as number):mean;
      const where=lv<lo?'below':lv>hi?'above':'within';
      const bad=where!=='within'&&((where==='above')===higherWorse);
      const range=`${lo.toFixed(dec)} to ${hi.toFixed(dec)}`;
      const msg=where==='within'
        ?`Latest ${lv.toFixed(dec)} ${unit} is within their usual range (${range}).`
        :`Latest ${lv.toFixed(dec)} ${unit} is ${where} their usual range (${range}).`;
      return(
        <View style={{marginHorizontal:16,marginBottom:10,padding:14,backgroundColor:C.card,borderRadius:16}}>
          <Text style={{color:C.sub,fontSize:12.5,fontWeight:'600'}}>{label}</Text>
          <Text style={{color:bad?C.orange:C.text,fontSize:13,marginTop:4,lineHeight:19}}>{msg}</Text>
          <View style={{height:H+8,marginTop:10}}>
            <View style={{position:'absolute',left:0,right:0,bottom:y(lo),height:Math.max(2,y(hi)-y(lo)),
                          backgroundColor:color+'22',borderRadius:4}}/>
            <View style={{position:'absolute',left:0,right:0,top:0,bottom:0,flexDirection:'row',alignItems:'flex-end',gap:3}}>
              {recent.map((x,i)=>{
                if(x.v==null)return<View key={i} style={{flex:1,height:2,backgroundColor:C.muted+'30'}}/>;
                const out=x.v<lo||x.v>hi;
                return(
                  <View key={i} style={{flex:1,height:Math.max(6,y(x.v)),justifyContent:'flex-start'}}>
                    <View style={{height:6,width:'100%',borderRadius:3,backgroundColor:out?C.orange:color}}/>
                  </View>
                );
              })}
            </View>
          </View>
          <Text style={{color:C.muted,fontSize:10,marginTop:6}}>
            {`Shaded band: their usual range from ${base.length} earlier days. Marks: the last ${recent.length} days.`}
          </Text>
        </View>
      );
    };

    const renderBaseline=()=>(
      <>
        <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginHorizontal:20,marginBottom:12}}>
          Each measure compared with this resident's own usual range. Orange means outside it.
        </Text>
        {history.length<6?(
          <View style={s.empty}><Text style={s.emptyTxt}>Baseline needs about a week</Text><Text style={s.emptySub}>Usual ranges appear once enough days are recorded</Text></View>
        ):(
          <>
            <Text style={s.secHeader}>Gait & Posture</Text>
            <BaselineCard k="camera_gait_speed_ms" label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
            <BaselineCard k="camera_cadence_spm" label="Steps per Minute" unit="steps/min" color={C.teal}/>
            <BaselineCard k="posture_score" label="Posture" unit="/100" color={C.purple}/>
            <BaselineCard k="camera_asymmetry_pct" label="Step Evenness (asymmetry)" unit="%" color={C.orange} dec={1} higherWorse/>
            <BaselineCard k="stride_variability" label="Walking Steadiness (step-time variation)" unit="%" color={C.teal} dec={1} higherWorse/>
            <Text style={s.secHeader}>Activity</Text>
            <BaselineCard k="activity_level_pct" label="Activity Level" unit="%" color={C.green}/>
            <BaselineCard k="active_minutes" label="Active Minutes" unit="min" color={C.green}/>
            <BaselineCard k="longest_stillness_min" label="Longest Stillness" unit="min" color={C.orange} higherWorse/>
            {history.some(r=>r.resting_hr!=null)&&(
              <>
                <Text style={s.secHeader}>Apple Watch</Text>
                <BaselineCard k="resting_hr" label="Resting Heart Rate" unit="bpm" color={C.red} higherWorse/>
                <BaselineCard k="hrv_sdnn" label="Heart Rate Variability" unit="ms" color={C.purple} dec={1}/>
                <BaselineCard k="sleep_hours" label="Sleep" unit="h" color={C.blue} dec={1}/>
              </>
            )}
          </>
        )}
      </>
    );

    return(
      <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
        <View style={s.pageHead}>
          <Text style={s.pageTitle}>Trends</Text>
          <Text style={s.pageSub}>{history.length} days recorded</Text>
        </View>
        <PersonChips people={namedPeople} selectedId={residentPerson?.id??null}
          serverUrl={urlRef.current} onPick={p=>{if(p)setSelectedPerson(p);}}/>
        <Text style={{color:C.muted,fontSize:12,lineHeight:17,marginHorizontal:16,marginBottom:12}}>
          {`These charts follow ${residentPerson?.name??(residentName||'the person most in view')}, measured by the camera. Tap anyone above to open their own trends.`}
        </Text>
        <View style={{flexDirection:'row',marginHorizontal:16,marginBottom:12,backgroundColor:C.card,borderRadius:12,padding:4}}>
          {([['week','7-Day'],['baseline','Baseline']] as const).map(([key,lbl])=>(
            <TouchableOpacity key={key} onPress={()=>setTrendView(key)}
              style={{flex:1,paddingVertical:9,borderRadius:9,alignItems:'center',
                      backgroundColor:trendView===key?C.green:'transparent'}}>
              <Text style={{color:trendView===key?'#000':C.sub,fontWeight:'700',fontSize:13}}>{lbl}</Text>
            </TouchableOpacity>
          ))}
        </View>
        {trendView==='baseline'?renderBaseline():history.length===0?(
          <View style={s.empty}><View style={s.emptyGlyph}><Text style={s.emptyGlyphTxt}>…</Text></View><Text style={s.emptyTxt}>No trend data yet</Text><Text style={s.emptySub}>Data collects automatically each day</Text></View>
        ):(
          <>
            <Text style={s.secHeader}>Activity — This Week</Text>
            <TrendBar data={week('activity_level_pct')} label="Activity Level" unit="%" color={C.green}/>
            <TrendBar data={week('active_minutes')} label="Active Minutes" unit="min" color={C.green}/>
            <TrendBar data={week('longest_stillness_min')} label="Longest Stillness" unit="min" color={C.orange}/>
            <Text style={s.secHeader}>Gait & Posture — This Week</Text>
            <TrendBar data={week('camera_gait_speed_ms')} label="Walking Speed" unit="m/s" color={C.teal} dec={2}/>
            <TrendBar data={week('posture_score')} label="Posture Score" unit="/100" color={C.purple}/>
            <TrendBar data={week('camera_asymmetry_pct')} label="Gait Asymmetry" unit="%" color={C.orange} dec={1}/>
            <TrendBar data={week('stride_variability')} label="Step-Time Variation (lower is steadier)" unit="%" color={C.teal} dec={1}/>
            {history.some(r=>r.hrv_sdnn!=null)&&(
              <>
                <Text style={s.secHeader}>Apple Watch — This Week</Text>
                <TrendBar data={week('hrv_sdnn')} label="HRV" unit="ms" color={C.purple} dec={1}/>
                <TrendBar data={week('resting_hr')} label="Resting HR" unit="bpm" color={C.red}/>
              </>
            )}
            <Text style={s.secHeader}>Daily Log</Text>
            <View style={{marginHorizontal:16,backgroundColor:C.card,borderRadius:16,overflow:'hidden',marginBottom:16}}>
              <View style={{flexDirection:'row',paddingHorizontal:12,paddingVertical:10,borderBottomWidth:1,borderBottomColor:C.border}}>
                {['Date','Active','Still','Speed','Posture','Flag'].map(h=><Text key={h} style={{flex:1,color:C.muted,fontSize:9,fontWeight:'700',textTransform:'uppercase'}}>{h}</Text>)}
              </View>
              {history.slice(0,14).map(row=>(
                <View key={row.date} style={{flexDirection:'row',paddingHorizontal:12,paddingVertical:10,borderBottomWidth:1,borderBottomColor:C.fill,backgroundColor:row.anomaly_flag?'#1a0800':'transparent'}}>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{row.date?.slice(5)}</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.active_minutes,0)}m</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.still_minutes,0)}m</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.camera_gait_speed_ms,2)}</Text>
                  <Text style={{flex:1,color:C.sub,fontSize:10}}>{fmt(row.posture_score,0)}</Text>
                  <Text style={{flex:1,color:row.anomaly_flag?C.orange:C.green,fontSize:10,fontWeight:'600'}}>{row.anomaly_flag?'Alert':'OK'}</Text>
                </View>
              ))}
            </View>
          </>
        )}
        <View style={{height:32}}/>
      </ScrollView>
    );
  };

  // ─────────────────────────────────────────────
  // ALERTS SCREEN
  // ─────────────────────────────────────────────
  const explainAlert=(a:AlertEntry):string=>{
    const p=a.payload||{};
    if(a.alert_type==='fall')return'A fall was detected: a rapid change from upright to lying down outside a bed or sofa. Check on the resident and confirm below that someone is responding.';
    if(a.alert_type==='bed_sitting')return`Sat up in bed during the night after resting about ${p.rested_min??'?'} minutes, and may be about to stand. A check-in now can prevent a fall.`;
    if(a.alert_type==='bed_exit')return`Got up during the night after resting about ${p.rested_min??'?'} minutes. Many falls happen getting out of bed at night, so a quick check-in may be worthwhile.`;
    if(a.alert_type==='stillness')return`No movement detected for over ${p.duration_min||'?'} minutes. This is outside the normal pattern for this resident.`;
    if(a.alert_type==='health_signal'&&p.metric&&p.value!=null&&p.baseline!=null){
      const pct=p.baseline!==0?Math.round(Math.abs((p.value-p.baseline)/p.baseline)*100):0;
      const dir=p.value>p.baseline?'above':'below';
      // SIGNALS_V2_APP: signals now judge a finished day (p.day)
      const when=p.day?(()=>{try{return 'on '+new Date(p.day+'T12:00:00').toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'});}catch(_e){return 'yesterday';}})():'today';
      return`${p.metric} ${when} (${Number(p.value).toFixed(1)}) was ${pct}% ${dir} this resident's 14-day baseline (${Number(p.baseline).toFixed(1)}).`;
    }
    return p.message??'An alert was triggered by VIGIL.';
  };

  const renderAlerts=()=>(
    <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <View style={s.pageHead}>
        <Text style={s.pageTitle}>Alerts</Text>
        <Text style={s.pageSub}>{alertCount>0?`${alertCount} unacknowledged`:'All clear'}</Text>
        <Text style={{color:C.muted,fontSize:12,marginTop:4,lineHeight:17}}>
          {`Camera alerts for ${residentPerson?.name??(residentName||'this room')}. Each alert shows who it is for.`}
        </Text>
      </View>
      {alerts.length===0?(
        <View style={s.empty}>
          <View style={s.emptyGlyph}><Text style={s.emptyGlyphTxt}>✓</Text></View>
          <Text style={s.emptyTxt}>No alerts</Text>
          <Text style={s.emptySub}>Falls, stillness events, and health anomalies appear here</Text>
        </View>
      ):alerts.map(a=>{
        const color = a.alert_type==='fall' ? C.red
          : a.alert_type==='stillness'        ? C.orange
          : a.alert_type==='bed_exit'         ? C.purple
          : a.alert_type==='bed_sitting'      ? C.purple
          : a.alert_type==='multiple_persons' ? C.purple
          : a.alert_type==='absence_pattern'  ? C.teal
          : C.blue;
        const badge = a.alert_type==='fall' ? 'FALL DETECTED'
          : a.alert_type==='stillness'        ? 'STILLNESS ALERT'
          : a.alert_type==='bed_exit'         ? 'NIGHT BED EXIT'
          : a.alert_type==='bed_sitting'      ? 'SITTING UP AT NIGHT'
          : a.alert_type==='multiple_persons' ? 'MULTIPLE PEOPLE'
          : a.alert_type==='absence_pattern'  ? 'ABSENCE ALERT'
          : 'HEALTH SIGNAL';
        const isExp=expandedAlert===a.id;
        return(
          <View key={a.id} style={{marginHorizontal:16,marginBottom:10,backgroundColor:C.card,borderRadius:16,padding:16,borderLeftWidth:3,borderLeftColor:color}}>
            <View style={{flexDirection:'row',justifyContent:'space-between',alignItems:'center'}}>
              <View style={{paddingHorizontal:10,paddingVertical:4,borderRadius:8,backgroundColor:color+'22',borderWidth:1,borderColor:color+'55'}}>
                <Text style={{color,fontSize:11,fontWeight:'700',letterSpacing:0.5}}>{badge}</Text>
              </View>
              {!a.seen&&<View style={{width:8,height:8,borderRadius:4,backgroundColor:color}}/>}
            </View>
            <Text style={{color:C.sub,fontSize:11,marginTop:6}}>{timeAgo(a.detected_at)}</Text>
            <Text style={{color:C.muted,fontSize:10,marginTop:1}}>{new Date(a.detected_at).toLocaleString()}</Text>
            {(()=>{
              const subj=namedPeople.find(p=>p.name===a.payload?.resident_name)??residentPerson;
              const nm=subj?.name??a.payload?.resident_name??(residentName||null);
              if(!nm)return null;
              return(
                <View style={{flexDirection:'row',alignItems:'center',gap:8,marginTop:8}}>
                  {subj&&<PersonAvatar personId={subj.id} name={subj.name} serverUrl={urlRef.current} size={26} isResident={!!subj.is_resident}/>}
                  <Text style={{color:C.sub,fontSize:12}}>{`For ${nm}`}</Text>
                </View>
              );
            })()}
            <View style={{marginTop:10,padding:12,backgroundColor:color+'12',borderRadius:10,borderLeftWidth:2,borderLeftColor:color}}>
              <Text style={{color:C.text,fontSize:13,lineHeight:20}}>{explainAlert(a)}</Text>
            </View>
            <View style={{flexDirection:'row',gap:12,marginTop:8}}>
              <TouchableOpacity onPress={()=>setExpandedAlert(isExp?null:a.id)}>
                <Text style={{color:C.muted,fontSize:11}}>{isExp?'Hide details':'Show details'}</Text>
              </TouchableOpacity>
              {a.alert_type==='fall'&&(
                <TouchableOpacity onPress={async()=>{
                  if(demoMode){setReplayMsg(null);setReplayFrames(demoReplay());return;}
                  try{
                    const r=await AX.get(`${url()}/alerts/${a.id}/replay`,{timeout:8000});
                    if(r.data?.available&&Array.isArray(r.data.frames)&&r.data.frames.length>1){setReplayMsg(null);setReplayFrames(r.data.frames);}
                    else{setReplayFrames([]);setReplayMsg('No replay was saved for this fall. Replays are kept for falls detected after this feature was added.');}
                  }catch(_e){setReplayFrames([]);setReplayMsg('Could not load the replay. Check the connection to the monitoring unit.');}
                }}>
                  <Text style={{color:C.red,fontSize:11,fontWeight:'600'}}>Watch replay</Text>
                </TouchableOpacity>
              )}
              {!a.seen&&(
                <TouchableOpacity onPress={()=>acknowledgeAlert(a.id)}>
                  <Text style={{color:C.green,fontSize:11,fontWeight:'600'}}>Acknowledge</Text>
                </TouchableOpacity>
              )}
              {!!a.seen&&!!(a as any).acknowledged_by&&(
                <Text style={{color:C.muted,fontSize:11}}>
                  {`Acknowledged by ${(a as any).acknowledged_by}`}
                </Text>
              )}
            </View>
            {isExp&&(
              <View style={{marginTop:8,gap:3}}>
                {Object.entries(a.payload||{}).map(([k,v])=>(
                  <Text key={k} style={{color:C.sub,fontSize:11}}>{k}: {String(v)}</Text>
                ))}
              </View>
            )}
          </View>
        );
      })}
      <View style={{height:32}}/>
    </ScrollView>
  );

  // ─────────────────────────────────────────────
  // SETTINGS SCREEN
  // ─────────────────────────────────────────────

  const renderResidentTab=()=>(
    <ScrollView keyboardShouldPersistTaps="handled" automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive">
      <View style={[s.settGrp,{marginTop:16}]}>
        <OField label="Resident Name" value={residentName} onChange={setResidentName} placeholder="Not set"/>
        <OField label="Height (cm)" value={residentHeight} onChange={setResidentHeight} placeholder="170" keyboard="number-pad" note="Used to calibrate gait speed measurements."/>
        <OField label="Emergency Phone" value={emergencyPhone} onChange={setEmergencyPhone} placeholder="+1 (206) 555-0000" keyboard="phone-pad" note="Receives SMS on fall or extended inactivity."/>
        <OField label="Your Name" value={responderName}
          onChange={(v:string)=>{setResponderName(v);AsyncStorage.setItem('vigil_caregiver_name',v.trim()).catch(()=>{});}}
          placeholder="e.g. Maria" keyboard="default"
          note="Recorded when you acknowledge an alert, so the record shows who responded."/>
        <OField label="Email" value={emergencyEmail} onChange={setEmergencyEmail} placeholder="Optional" keyboard="email-address"/>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}>
          <View>
            <Text style={{color:C.text,fontSize:15}}>Apple Watch</Text>
            <Text style={{color:C.muted,fontSize:11,marginTop:2}}>Show Apple Watch metrics on dashboard</Text>
          </View>
          <Switch value={watchEnabled} onValueChange={setWatchEnabled} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFFFFF"/>
        </View>
      </View>
      <TouchableOpacity style={[s.btnPrimary,{marginHorizontal:16,marginTop:20,opacity:savingProfile?0.6:1}]} onPress={saveProfile} disabled={savingProfile}>
        {savingProfile?<ActivityIndicator color="#000" size="small"/>:<Text style={s.btnPrimaryTxt}>Save Profile</Text>}
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,textAlign:'center',marginTop:10,marginHorizontal:16}}>
        Profile is synced to your VIGIL server and used for emergency alert messages.
      </Text>
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderServerTab=()=>(
    <ScrollView automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <View style={[s.card,{marginHorizontal:16,marginTop:16,alignItems:'center',paddingVertical:28}]}>
        <View style={{width:16,height:16,borderRadius:8,marginBottom:12,backgroundColor:jetsonOk===true?C.green:jetsonOk===false?C.red:C.sub}}/>
        <Text style={{color:C.text,fontSize:17,fontWeight:'700',marginBottom:4}}>
          {jetsonOk===true?'Connected to VIGIL Server':jetsonOk===false?'Server Unreachable':'Checking...'}
        </Text>
        <Text style={{color:C.sub,fontSize:13}}>{serverIp} · Port {serverPort}</Text>
        {serverVersion&&<Text style={{color:C.muted,fontSize:11,marginTop:4}}>Server {serverVersion}</Text>}
      </View>
      <TouchableOpacity style={[{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,marginHorizontal:16,marginTop:16,backgroundColor:C.card,borderRadius:12}]} onPress={()=>setAdvancedOpen(p=>!p)}>
        <Text style={{color:C.text,fontSize:15}}>Advanced Connection Settings</Text>
        <Text style={{color:C.sub,fontSize:13}}>{advancedOpen?'Hide':'Show'}</Text>
      </TouchableOpacity>
      {advancedOpen&&(
        <View style={[s.settGrp,{marginTop:8}]}>
          <OField label="IP Address" value={serverIp} onChange={setServerIp} placeholder={DEFAULT_IP} keyboard="numbers-and-punctuation"/>
          <OField label="Port" value={serverPort} onChange={setServerPort} placeholder={DEFAULT_PORT} keyboard="number-pad"/>
          <TouchableOpacity style={[s.btnPrimary,{margin:16}]} onPress={reconnect}><Text style={s.btnPrimaryTxt}>Reconnect</Text></TouchableOpacity>
        </View>
      )}
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderCameraTab=()=>(
    <ScrollView showsVerticalScrollIndicator={false} automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <Text style={s.secHeader}>Camera Status</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}>
          <Text style={{color:C.text,fontSize:15}}>Connection</Text>
          <View style={{flexDirection:'row',alignItems:'center',gap:6}}>
            <View style={{width:7,height:7,borderRadius:4,backgroundColor:cameraStatus?.online?C.green:C.red}}/>
            <Text style={{color:cameraStatus?.online?C.green:C.red,fontSize:14,fontWeight:'500'}}>{cameraStatus?.online?'Online':'Offline'}</Text>
          </View>
        </View>
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
          <Text style={{color:C.text,fontSize:15}}>Person Detected</Text>
          <Text style={{color:cameraStatus?.person_present?C.green:C.sub,fontSize:14,fontWeight:'500'}}>{cameraStatus?.person_present?'Yes':'No'}</Text>
        </View>
        {(cameraStatus?.person_count??0)>1&&(
          <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
            <Text style={{color:C.text,fontSize:15}}>People in Frame</Text>
            <Text style={{color:C.orange,fontSize:14,fontWeight:'500'}}>{String(cameraStatus?.person_count)} · tracking largest</Text>
          </View>
        )}
        <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}>
          <Text style={{color:C.text,fontSize:15}}>Frame Rate</Text>
          <Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{cameraStatus?.fps!=null?`${cameraStatus.fps.toFixed(1)} fps`:'—'}</Text>
        </View>
      </View>
      <Text style={s.secHeader}>Live View</Text>
      <View style={{marginHorizontal:16}}>{renderCameraFeed()}</View>
      <Text style={{color:C.muted,fontSize:11,marginLeft:16,marginTop:6,marginBottom:4}}>Live view · Tap for full screen</Text>
      <Text style={s.secHeader}>Room setup</Text>
      <TouchableOpacity disabled={demoMode} onPress={()=>setZoneEditor(true)} style={[s.btnPrimary,{marginHorizontal:16,opacity:demoMode?0.4:1}]}>
        <Text style={s.btnPrimaryTxt}>Set up bed and couch zones</Text>
      </TouchableOpacity>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginTop:8,lineHeight:18}}>Do this once when the camera is installed, and again if it is moved.</Text>
      <Text style={s.secHeader} onLayout={()=>{if(idMode===null&&!demoMode)loadIdMode();}}>Who VIGIL recognises</Text>
      <View style={{flexDirection:'row',marginHorizontal:16,backgroundColor:C.card,borderRadius:14,padding:4,borderWidth:1,borderColor:C.border}}>
        {([['resident','One resident'],['family','Everyone']] as const).map(([k,lbl])=>(
          <TouchableOpacity key={k} disabled={demoMode}
            onPress={()=>{
              if(k==='family'){saveIdMode('family');return;}
              const named=persons.filter(x=>!!x.name);
              if(!named.length){Alert.alert('Name the resident first','Name the resident from Today or Alerts, then choose this.');return;}
              Alert.alert('Who is the resident?','Only this person is named and measured. Everyone else is shown as Visitor.',[
                ...named.slice(0,6).map(x=>({text:x.name as string,onPress:()=>saveIdMode('resident',x.id)})),
                {text:'Cancel',style:'cancel' as const},
              ]);
            }}
            style={{flex:1,paddingVertical:10,borderRadius:10,alignItems:'center',backgroundColor:idMode===k?C.green:'transparent'}}>
            <Text style={{color:idMode===k?'#04140A':C.sub,fontWeight:'700',fontSize:14}}>{lbl}</Text>
          </TouchableOpacity>
        ))}
      </View>
      <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginTop:8,lineHeight:18}}>
        {idMode==='resident'
          ?'Only the resident is named and their health measured. Staff, family and visitors appear as Visitor, with no prompts.'
          :'Everyone in the home is named. Tap a name under In view now to confirm or correct it; VIGIL remembers each look.'}
      </Text>
      <Text style={s.secHeader}>Setup Guide</Text>
      {[
        {n:'1',title:'Place the Camera',desc:'Mount the camera 5–7 feet high, angled slightly downward. Position it to see the resident\'s full body when standing in their main living area.'},
        {n:'2',title:'Connect to Wi-Fi',desc:'Open the Tapo or Reolink app, add your camera, and connect it to the same Wi-Fi network as the VIGIL server.'},
        {n:'3',title:'Enable RTSP Stream',desc:'In the camera app, go to Advanced Settings and enable RTSP. Note the camera IP address.'},
        {n:'4',title:'Start Monitoring',desc:'Once the unit is plugged in and connected, monitoring starts on its own. VIGIL spends the first week learning what is normal for the resident.'},
      ].map(step=>(
        <View key={step.n} style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',gap:14}]}>
          <View style={{width:28,height:28,borderRadius:14,backgroundColor:C.green+'22',alignItems:'center',justifyContent:'center'}}>
            <Text style={{color:C.green,fontWeight:'700',fontSize:13}}>{step.n}</Text>
          </View>
          <View style={{flex:1}}>
            <Text style={{color:C.text,fontSize:15,fontWeight:'600',marginBottom:4}}>{step.title}</Text>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19}}>{step.desc}</Text>
          </View>
        </View>
      ))}
      {persons.length>0&&(
        <>
          <Text style={s.secHeader}>Monitored People</Text>
          <Text style={{color:C.muted,fontSize:12,marginHorizontal:16,marginBottom:8,lineHeight:17}}>
            Everyone VIGIL has recognised. Tap Edit to rename someone, mark them as a visitor, or merge a duplicate.
          </Text>
          {persons.map(p=>(
            <TouchableOpacity
              key={p.id}
              disabled={!p.name}
              activeOpacity={p.name?0.6:1}
              onPress={()=>{if(p.name)setSelectedPerson(p);}}
              style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',alignItems:'center',justifyContent:'space-between'}]}>
              <View style={{flexDirection:'row',alignItems:'center',gap:10,flex:1,minWidth:0}}>
                <PersonAvatar personId={p.id} name={p.name} serverUrl={urlRef.current} size={48} isResident={!!p.is_resident}/>
                <View style={{flex:1,minWidth:0}}>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}} numberOfLines={1} ellipsizeMode="tail">{p.name??'Unidentified Person'}</Text>
                  <Text style={{color:C.sub,fontSize:11,marginTop:2}} numberOfLines={1} ellipsizeMode="tail">
                    {p.is_resident?'Resident':'Visitor'}{` · ${Math.round(p.confidence*100)}%`}{p.name?' · Tap for trends':''}
                  </Text>
                </View>
              </View>
              {!p.name?(
                <TouchableOpacity
                  style={{backgroundColor:C.green,paddingHorizontal:12,paddingVertical:6,borderRadius:8}}
                  onPress={()=>{
                    setPendingPersonAlert({id:0,alert_type:'manual',room_id:'room_1',person_ids:[p.id],resolved:0,created_at:''});
                    setShowPersonModal(true);
                  }}>
                  <Text style={{color:'#000',fontSize:12,fontWeight:'600'}}>Identify</Text>
                </TouchableOpacity>
              ):(
                <View style={{flexDirection:'row',alignItems:'center',gap:12}}>
                  <TouchableOpacity hitSlop={{top:10,bottom:10,left:10,right:10}} onPress={()=>editPerson(p)}>
                    <Text style={{color:C.blue,fontSize:13,fontWeight:'700'}}>Edit</Text>
                  </TouchableOpacity>
                  <Text style={{color:C.muted,fontSize:16}}>{'>'}</Text>
                </View>
              )}
            </TouchableOpacity>
          ))}
        </>
      )}
      {rooms.length>0&&(
        <>
          <Text style={s.secHeader}>Active Rooms ({String(rooms.length)})</Text>
          {rooms.map(r=>(
            <View key={r.id} style={[s.card,{marginHorizontal:16,marginBottom:8,flexDirection:'row',alignItems:'center',justifyContent:'space-between'}]}>
              <View style={{flexDirection:'row',alignItems:'center',gap:10}}>
                <View style={{width:8,height:8,borderRadius:4,backgroundColor:r.camera_online?C.green:C.muted}}/>
                <View>
                  <Text style={{color:C.text,fontSize:14,fontWeight:'600'}}>{r.name}</Text>
                  {r.resident_name&&<Text style={{color:C.sub,fontSize:11,marginTop:2}}>{r.resident_name}</Text>}
                </View>
              </View>
              <Text style={{color:r.camera_online?C.green:C.muted,fontSize:12}}>{r.camera_online?`${r.fps?.toFixed(0)??'?'} fps`:'Offline'}</Text>
            </View>
          ))}
        </>
      )}
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderSupportTab=()=>(
    <ScrollView automaticallyAdjustKeyboardInsets keyboardDismissMode="interactive" keyboardShouldPersistTaps="handled">
      <Text style={s.secHeader}>App Info</Text>
      <View style={s.settGrp}>
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}}><Text style={{color:C.text,fontSize:15}}>App Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>VIGIL v1.0.0</Text></View>
        {serverVersion&&<View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>Server Version</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{serverVersion}</Text></View>}
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>HealthKit</Text><Text style={{color:initialized?C.green:initError?C.orange:C.sub,fontSize:14,fontWeight:'500'}}>{initialized?'Connected':initError?'Not connected':'Starting'}</Text></View>
        <View style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}><Text style={{color:C.text,fontSize:15}}>Active Rooms</Text><Text style={{color:C.sub,fontSize:14,fontWeight:'500'}}>{String(rooms.length||1)}</Text></View>
      </View>
      <Text style={s.secHeader}>Sync Apple Watch</Text>
      <View style={[s.card,{marginHorizontal:16}]}>
        <TouchableOpacity style={[s.btnPrimary,{opacity:syncing||!initialized||!jetsonOk?0.5:1}]} onPress={()=>performSync(false)} disabled={syncing||!initialized||jetsonOk!==true}>
          {syncing?<View style={{flexDirection:'row',alignItems:'center',justifyContent:'center',gap:8}}><ActivityIndicator color="#000" size="small"/><Text style={s.btnPrimaryTxt}>Syncing...</Text></View>:<Text style={s.btnPrimaryTxt}>Sync Apple Watch Now</Text>}
        </TouchableOpacity>
        {lastSync&&<Text style={{color:C.muted,fontSize:11,textAlign:'center',marginTop:8}}>Last synced {lastSync}  ·  +{lastIns??0} new  ·  {lastSkip??0} skipped</Text>}
      </View>
      <Text style={s.secHeader}>Diagnostics</Text>
      <View style={s.settGrp}>
        <TouchableOpacity style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14}} onPress={()=>setShowLogs(p=>!p)}>
          <Text style={{color:C.text,fontSize:15}}>Debug Logs</Text>
          <Text style={{color:C.sub,fontSize:13}}>{showLogs?'Hide':'Show ('+debugLog.length+')'}</Text>
        </TouchableOpacity>
        {showLogs&&(
          <View style={{padding:12}}>
            <ScrollView style={{maxHeight:280,backgroundColor:'#000',borderRadius:8,padding:8}} nestedScrollEnabled>
              {debugLog.length===0?<Text style={{color:C.muted,fontSize:11}}>No log entries.</Text>:debugLog.map((e,i)=>(
                <Text key={i} style={{fontSize:10,fontFamily:'Courier New',lineHeight:16,color:e.level==='ERROR'?C.red:e.level==='WARN'?C.orange:e.level==='OK'?C.green:C.sub}}>{e.ts}  [{e.level}]  {e.msg}</Text>
              ))}
            </ScrollView>
            <TouchableOpacity style={[s.btnSecondary,{marginTop:8}]} onPress={()=>setDebugLog([])}><Text style={s.btnSecondaryTxt}>Clear Logs</Text></TouchableOpacity>
          </View>
        )}
        <TouchableOpacity style={{flexDirection:'row',justifyContent:'space-between',paddingHorizontal:16,paddingVertical:14,borderTopWidth:1,borderTopColor:C.hair}}
          onPress={()=>Alert.alert('Reset Onboarding','This will delete all saved settings and restart the setup flow.',
            [{text:'Cancel',style:'cancel'},{text:'Reset',style:'destructive',onPress:async()=>{await (AsyncStorage as any).multiRemove([SK_ONBOARDING,SK_PROFILE,SK_SERVER]);setOnboarding(true);}}])}>
          <Text style={{color:C.red,fontSize:15}}>Reset Onboarding</Text>
          <Text style={{color:C.muted,fontSize:13}}>Restart setup</Text>
        </TouchableOpacity>
      </View>
      <View style={{height:32}}/>
    </ScrollView>
  );

  const renderSettings=()=>(
    <View style={{flex:1}}>
      <View style={s.pageHead}><Text style={s.pageTitle}>Settings</Text></View>
      <View style={{flexDirection:'row',borderBottomWidth:1,borderBottomColor:C.border,paddingHorizontal:16}}>
        {(['resident','server','camera','support'] as SettingTab[]).map(t=>(
          <TouchableOpacity key={t} style={{flex:1,alignItems:'center',paddingVertical:10}} onPress={()=>setSettingsTab(t)}>
            <Text style={{color:settingsTab===t?C.green:C.sub,fontSize:13,fontWeight:'600'}}>{t.charAt(0).toUpperCase()+t.slice(1)}</Text>
            {settingsTab===t&&<View style={{position:'absolute',bottom:0,left:0,right:0,height:2,backgroundColor:C.green}}/>}
          </TouchableOpacity>
        ))}
      </View>
      <View style={{flex:1}}>
        {settingsTab==='resident'&&renderResidentTab()}
        {settingsTab==='server'&&renderServerTab()}
        {settingsTab==='camera'&&renderCameraTab()}
        {settingsTab==='support'&&renderSupportTab()}
      </View>
    </View>
  );

  // ─────────────────────────────────────────────
  // BOOT / ONBOARDING STATES
  // ─────────────────────────────────────────────
  if(booting){
    return(
      <View style={{flex:1,backgroundColor:C.bg,alignItems:'center',justifyContent:'center'}}>
        <StatusBar barStyle="light-content"/>
        <Text style={{color:C.green,fontSize:42,fontWeight:'800',letterSpacing:4}}>VIGIL</Text>
        <Text style={{color:C.muted,fontSize:13,marginTop:8,letterSpacing:2}}>HEALTH MONITOR</Text>
        <ActivityIndicator color={C.green} style={{marginTop:32}}/>
      </View>
    );
  }

  if(showOnboarding){
    return(
      <OnboardingScreen onDemo={()=>{setOnboarding(false);enterDemo();}} onComplete={(profile,ip,port)=>{
        setResidentName(profile.residentName);setResidentHeight(profile.residentHeight);
        setEmergencyPhone(profile.emergencyPhone);setEmergencyEmail(profile.emergencyEmail);
        setWatchEnabled(profile.watchEnabled);
        setServerIp(ip);setServerPort(port);
        const u=`http://${ip}:${port}`;urlRef.current=u;
        setOnboarding(false);
      }}/>
    );
  }

  // ─────────────────────────────────────────────
  // MAIN RENDER
  // ─────────────────────────────────────────────
  const TABS:[MainTab,string,string][]=[
    ['today','Today','⊙'],
    ['trends','Trends','↗'],
    ['alerts','Alerts','⚑'],
    ['insights','AI','◈'],
    ['settings','Settings','⋯'],
  ];

  return(
    <SafeAreaView style={s.root}>
      <StatusBar barStyle="light-content"/>

      {/* Fullscreen camera modal */}
      <Modal visible={fullscreen} animationType="fade" statusBarTranslucent>
        <View style={{flex:1,backgroundColor:'#000',alignItems:'center',justifyContent:'center'}}>
          <StatusBar barStyle="light-content" backgroundColor="#000"/>
          {cameraFrame?(
            <Image source={{uri:`data:image/jpeg;base64,${cameraFrame}`}} style={{width:SW,height:SW*9/16}} resizeMode="contain" fadeDuration={0}/>
          ):(
            <Text style={{color:C.muted,fontSize:16}}>No camera feed</Text>
          )}
          <TouchableOpacity
            style={{position:'absolute',top:56,right:20,backgroundColor:'rgba(0,0,0,0.7)',borderRadius:20,paddingHorizontal:16,paddingVertical:8}}
            onPress={()=>setFullscreen(false)}>
            <Text style={{color:'#fff',fontSize:14,fontWeight:'700'}}>Close</Text>
          </TouchableOpacity>
          {cameraFrame&&(
            <View style={{position:'absolute',top:60,left:20,backgroundColor:'rgba(0,0,0,0.7)',borderRadius:8,flexDirection:'row',alignItems:'center',paddingHorizontal:10,paddingVertical:5,gap:6}}>
              <Animated.View style={{width:6,height:6,borderRadius:3,backgroundColor:C.green,transform:[{scale:LIVE_PULSE}]}}/>
              <Text style={{color:'#fff',fontSize:12,fontWeight:'700',letterSpacing:1}}>LIVE</Text>
            </View>
          )}
          <Text style={{position:'absolute',bottom:48,color:C.muted,fontSize:12}}>
            {cameraStatus?.person_present?'Resident Detected':'Room Empty'} · {cameraStatus?.fps?.toFixed(1)??'?'} fps
          </Text>
        </View>
      </Modal>

      <Modal visible={replayFrames!==null} animationType="slide" transparent statusBarTranslucent onRequestClose={()=>setReplayFrames(null)}>
        <View style={{flex:1,backgroundColor:'rgba(0,0,0,0.85)',justifyContent:'flex-end'}}>
          <View style={{backgroundColor:C.card,borderTopLeftRadius:24,borderTopRightRadius:24,padding:24,paddingBottom:44}}>
            <View style={s.sheetHandle}/>
            <Text style={{color:C.text,fontSize:20,fontWeight:'800',marginBottom:4}}>Fall replay</Text>
            <Text style={{color:C.sub,fontSize:13,lineHeight:19,marginBottom:14}}>
              {demoMode?'Sample data. ':''}The 30 seconds before the alert, as a stick figure. VIGIL never stores video.
            </Text>
            {replayFrames&&replayFrames.length>1?<FallReplayPlayer frames={replayFrames}/>:
              <Text style={{color:C.sub,fontSize:14,lineHeight:20}}>{replayMsg??''}</Text>}
            <TouchableOpacity onPress={()=>setReplayFrames(null)} style={{marginTop:18,paddingVertical:13,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
              <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Close</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>

      {/* NEW_PERSON_SHEET_V2: one sheet per new person, photo first, name field
          already open. Wrapped in KeyboardAvoidingView so the keyboard pushes
          the sheet up instead of covering what is being typed. */}
      <Modal visible={showPersonModal && !!pendingPersonAlert} animationType="slide" transparent statusBarTranslucent
        onRequestClose={()=>{setShowPersonModal(false);setPendingPersonAlert(null);}}>
        <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios'?'padding':'height'}>
          <View style={{flex:1,backgroundColor:'rgba(0,0,0,0.85)',justifyContent:'flex-end'}}>
            <View style={{backgroundColor:C.card,borderTopLeftRadius:24,borderTopRightRadius:24,maxHeight:'92%'}}>
              <ScrollView keyboardShouldPersistTaps="handled" keyboardDismissMode="interactive"
                contentContainerStyle={{padding:24,paddingBottom:40}} showsVerticalScrollIndicator={false}>
                <View style={s.sheetHandle}/>
                <Text style={{color:C.text,fontSize:22,fontWeight:'800',marginBottom:6}}>New person detected</Text>
                <Text style={{color:C.sub,fontSize:14,lineHeight:20,marginBottom:18}}>
                  Who is this? Naming them once is enough; VIGIL will not ask about them again.
                </Text>
                {(pendingPersonAlert?.person_ids??[])
                  .filter(pid=>!persons.find(p=>p.id===pid&&!!p.name))
                  .slice(0,1)
                  .map(pid=>{
                  const known=persons.filter(p=>p.name&&p.name!=='Visitor'&&p.id!==pid);
                  const save=(v?:string)=>{const n=((v??nameRef.current)||newPersonName).trim();if(n){nameRef.current='';identifyPerson(pid,n,isResidentFlag,pendingPersonAlert?.id);}};
                  return(
                    <View key={pid}>
                      <PersonPhotoCard personId={pid} serverUrl={urlRef.current} large/>
                      <KInput
                        style={{backgroundColor:C.bg,color:C.text,fontSize:18,paddingHorizontal:16,paddingVertical:14,
                                borderRadius:14,borderWidth:1.5,borderColor:C.green+'66',marginTop:6}}
                        placeholder="Their name" placeholderTextColor={C.muted}
                        value={newPersonName} onChange={setNewPersonName} liveRef={nameRef}
                        autoCapitalize="words" autoCorrect={false} returnKeyType="done"
                        onSubmit={(v:string)=>save(v)}/>
                      <View style={{flexDirection:'row',gap:10,alignItems:'center',marginTop:12}}>
                        <Switch value={isResidentFlag} onValueChange={setIsResidentFlag} trackColor={{false:C.fill,true:C.green}} thumbColor="#FFF"/>
                        <Text style={{color:C.sub,fontSize:13,flex:1}}>This is the resident being monitored</Text>
                      </View>
                      <TouchableOpacity onPress={()=>save()} disabled={newPersonName.trim().length===0&&!nameRef.current.trim()}
                        style={{backgroundColor:C.green,borderRadius:12,paddingVertical:14,alignItems:'center',marginTop:14,
                                opacity:newPersonName.trim().length>0?1:0.45}}>
                        <Text style={{color:'#000',fontWeight:'800',fontSize:15}}>Save name</Text>
                      </TouchableOpacity>
                      {known.length>0&&(
                        <View style={{marginTop:18}}>
                          <Text style={{color:C.muted,fontSize:12,marginBottom:8}}>Or, if this is someone VIGIL already knows:</Text>
                          <View style={{flexDirection:'row',gap:8,flexWrap:'wrap'}}>
                            {known.map(p=>(
                              <TouchableOpacity key={p.id} onPress={()=>mergePerson(pid,p.id,pendingPersonAlert?.id)}
                                style={{backgroundColor:C.fill,paddingHorizontal:12,paddingVertical:8,borderRadius:10,borderWidth:1,borderColor:C.border}}>
                                <Text style={{color:C.text,fontSize:13,fontWeight:'600'}}>{`Same as ${p.name}`}</Text>
                              </TouchableOpacity>
                            ))}
                          </View>
                        </View>
                      )}
                      <View style={{flexDirection:'row',gap:10,marginTop:18}}>
                        <TouchableOpacity onPress={()=>identifyPerson(pid,'Visitor',false,pendingPersonAlert?.id)}
                          style={{flex:1,paddingVertical:12,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
                          <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Visitor</Text>
                        </TouchableOpacity>
                        <TouchableOpacity
                          onPress={()=>{
                            if(pendingPersonAlert?.id)snoozedRef.current[pendingPersonAlert.id]=Date.now()+30*60*1000;
                            setShowPersonModal(false);setPendingPersonAlert(null);setNewPersonName('');
                          }}
                          style={{flex:1,paddingVertical:12,borderRadius:12,alignItems:'center',borderWidth:1,borderColor:C.border}}>
                          <Text style={{color:C.sub,fontWeight:'600',fontSize:14}}>Not now</Text>
                        </TouchableOpacity>
                      </View>
                    </View>
                  );
                })}
                {(pendingPersonAlert?.person_ids??[]).every(pid=>!!persons.find(p=>p.id===pid&&!!p.name))&&(
                  <TouchableOpacity onPress={()=>{setShowPersonModal(false);setPendingPersonAlert(null);
                      if(pendingPersonAlert?.id)AX.post(`${urlRef.current||FALLBACK_URL}/persons/alerts/${pendingPersonAlert.id}/resolve`,{}).catch(()=>{});}}
                    style={{padding:14,alignItems:'center'}}>
                    <Text style={{color:C.sub,fontSize:14}}>Already identified. Close</Text>
                  </TouchableOpacity>
                )}
              </ScrollView>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>

      <Modal visible={zoneEditor} animationType="slide" presentationStyle="fullScreen" onRequestClose={()=>setZoneEditor(false)}>
        {zoneEditor&&<ZoneEditor serverUrl={urlRef.current} onClose={()=>setZoneEditor(false)}/>}
      </Modal>

      {/* Person detail screen */}
      <Modal visible={!!selectedPerson} animationType="slide" presentationStyle="fullScreen">
        {selectedPerson&&(
          <PersonDetailScreen demoMode={demoMode}
            personId={selectedPerson.id}
            personName={selectedPerson.name??'Unidentified Person'}
            serverUrl={urlRef.current}
            onClose={()=>setSelectedPerson(null)}
          />
        )}
      </Modal>

      {/* Main app */}
      <View style={{flex:1}}>
        {demoMode&&(
          <View style={{flexDirection:'row',alignItems:'center',justifyContent:'space-between',
                        backgroundColor:C.orange+'22',borderBottomWidth:1,
                        borderBottomColor:C.orange+'55',paddingHorizontal:16,paddingVertical:8}}>
            <Text style={{color:C.orange,fontSize:11,fontWeight:'700',flex:1,letterSpacing:0.3}}>
              DEMO MODE — sample data, not a live system
            </Text>
            <TouchableOpacity onPress={exitDemo}
              style={{paddingHorizontal:10,paddingVertical:4,borderRadius:8,
                      borderWidth:1,borderColor:C.orange+'77'}}>
              <Text style={{color:C.orange,fontSize:11,fontWeight:'700'}}>Exit</Text>
            </TouchableOpacity>
          </View>
        )}
        {activeTab==='today'&&renderToday()}
        {activeTab==='trends'&&renderTrends()}
        {activeTab==='alerts'&&renderAlerts()}
        {activeTab==='insights'&&<AIInsightsScreen serverUrl={urlRef.current} demoMode={demoMode} people={persons}/>}
        {activeTab==='settings'&&renderSettings()}
      </View>

      {/* Tab bar */}
      <View style={s.tabBar}>
        {TABS.map(([key,label,icon])=>{
          const active=activeTab===key; const color=active?C.green:C.muted;
          return(
            <TouchableOpacity key={key} style={{flex:1,alignItems:'center',paddingTop:4,paddingBottom:2}} onPress={()=>setActiveTab(key)}>
              {active&&<View style={s.tabIndicator}/>}
              <View style={{position:'relative'}}>
                <TabIcon name={key} color={color}/>
                {key==='alerts'&&alertCount>0&&(
                  <View style={s.badge}><Text style={s.badgeTxt}>{alertCount>9?'9+':String(alertCount)}</Text></View>
                )}
              </View>
              <Text style={{fontSize:11,color,fontWeight:active?'700':'600',marginTop:3}}>{label}</Text>
            </TouchableOpacity>
          );
        })}
      </View>
    </SafeAreaView>
  );
}

// ─────────────────────────────────────────────
// STYLES
// ─────────────────────────────────────────────
const s=StyleSheet.create({
  root:         {flex:1,backgroundColor:C.bg},
  pageHead:     {paddingHorizontal:20,paddingTop:20,paddingBottom:12},
  pageTitle:    {color:C.text,fontSize:36,fontWeight:'800',letterSpacing:-1.1},
  pageSub:      {color:C.sub,fontSize:15,marginTop:4},
  secHeader:    {color:C.text,fontSize:21,fontWeight:'800',letterSpacing:-0.5,marginLeft:20,marginTop:30,marginBottom:12},
  pillRow:      {flexDirection:'row',paddingHorizontal:16,gap:8,marginBottom:4,flexWrap:'wrap'},
  pill:         {flexDirection:'row',alignItems:'center',backgroundColor:'rgba(50,215,75,0.07)',borderRadius:100,paddingHorizontal:12,paddingVertical:7,gap:7,borderWidth:1,borderColor:'rgba(50,215,75,0.22)'},
  pillDot:      {width:7,height:7,borderRadius:4},
  pillTxt:      {color:'#CFEFD6',fontSize:12.5,fontWeight:'700'},
  card:         {backgroundColor:C.card,borderRadius:20,padding:18,borderWidth:1,borderColor:C.border},
  grid:         {flexDirection:'row',flexWrap:'wrap',paddingHorizontal:12,gap:10},
  metCard:      {width:(SW-44)/2,backgroundColor:C.card,borderRadius:20,padding:16,borderWidth:1,borderColor:C.border},
  metTop:       {flexDirection:'row',alignItems:'center',gap:6,marginBottom:10},
  metDot:       {width:7,height:7,borderRadius:4},
  metLbl:       {color:C.sub,fontSize:12.5,fontWeight:'600'},
  metVal:       {color:C.text,fontSize:28,fontWeight:'800',letterSpacing:-0.8,fontVariant:['tabular-nums']},
  metUnit:      {color:C.sub,fontSize:11,marginTop:3},
  warnChip:     {alignSelf:'flex-start',marginTop:8,backgroundColor:'rgba(255,69,58,0.14)',borderRadius:100,paddingHorizontal:8,paddingVertical:3},
  warnChipTxt:  {color:C.red,fontSize:9,fontWeight:'700',letterSpacing:0.4},
  empty:        {alignItems:'center',paddingVertical:56,paddingHorizontal:32},
  emptyGlyph:   {width:64,height:64,borderRadius:20,backgroundColor:'rgba(50,215,75,0.12)',alignItems:'center',justifyContent:'center',marginBottom:16},
  emptyGlyphTxt:{color:C.green,fontSize:24,fontWeight:'800'},
  emptyTxt:     {color:C.text,fontSize:16,fontWeight:'600'},
  emptySub:     {color:C.muted,fontSize:13,textAlign:'center',marginTop:5,lineHeight:19},
  settGrp:      {marginHorizontal:16,backgroundColor:C.card,borderRadius:20,overflow:'hidden',borderWidth:1,borderColor:C.border},
  btnPrimary:   {backgroundColor:C.green,borderRadius:100,paddingVertical:16,paddingHorizontal:20,alignItems:'center',shadowColor:C.green,shadowOpacity:0.4,shadowRadius:16,shadowOffset:{width:0,height:6}},
  btnPrimaryTxt:{color:'#04140A',fontSize:16,fontWeight:'800'},
  btnSecondary: {backgroundColor:'rgba(255,255,255,0.04)',borderRadius:100,paddingVertical:14,paddingHorizontal:20,alignItems:'center',borderWidth:1,borderColor:C.border},
  btnSecondaryTxt:{color:C.text,fontSize:14,fontWeight:'600'},
  tabBar:       {flexDirection:'row',backgroundColor:'#070B09',borderTopWidth:1,borderTopColor:C.hair,paddingBottom:22,paddingTop:10},
  tabIndicator: {position:'absolute',top:-10,alignSelf:'center',width:28,height:3,borderRadius:2,backgroundColor:C.green,shadowColor:C.green,shadowOpacity:0.8,shadowRadius:6,shadowOffset:{width:0,height:0}},
  badge:        {position:'absolute',top:-3,right:-8,backgroundColor:C.red,borderRadius:7,minWidth:15,height:15,alignItems:'center',justifyContent:'center',paddingHorizontal:3,borderWidth:1.5,borderColor:'#070B09'},
  badgeTxt:     {color:'#fff',fontSize:9,fontWeight:'800'},
  avatar:       {width:44,height:44,borderRadius:22,backgroundColor:C.fill,alignItems:'center',justifyContent:'center',borderWidth:1,borderColor:C.border},
  avatarTxt:    {color:C.green,fontSize:18,fontWeight:'800'},
  sheetHandle:  {alignSelf:'center',width:40,height:4,borderRadius:2,backgroundColor:C.fill,marginBottom:16},
});
