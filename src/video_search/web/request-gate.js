(function(root){
  let latest=0;
  root.latestRequest={
    begin:function(){latest+=1;return latest;},
    isCurrent:function(request){return request===latest;},
    run:async function(task,onSuccess,onError){
      const request=this.begin();
      try{
        const value=await task();
        if(this.isCurrent(request))onSuccess(value);
      }catch(error){
        if(this.isCurrent(request))onError(error);
      }
    }
  };
  root.searchLocation=function(query){return '/?q='+encodeURIComponent(query)};
  root.queryFromLocation=function(location){
    if(location.pathname!=='/')return null;
    return new URLSearchParams(location.search).get('q');
  };
})(typeof globalThis==='undefined'?window:globalThis);
